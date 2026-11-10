"""Ejecución de lotes: cola con concurrencia controlada y reintentos seguros.

Garantías (requisitos GEN-11 y GEN-15):

- Concurrencia POR LOTE: cada lote corre con la cifra elegida en la UI
  (``BatchConfig.concurrency``, selector 1-4 con default 2), no con la del
  entorno. Los jobs sobrantes esperan en la cola del lote y se lanzan al
  liberarse capacidad (ventana deslizante), sin nuevos clics ni duplicados.
- Techo global de seguridad: el proceso nunca supera 8 jobs activos, ni
  siquiera con varios lotes en paralelo. ``VISUALFORGE_CONCURRENCY`` (1-8)
  es preferencia/valor de arranque: default ante configuraciones inválidas y
  sugerencia inicial de la UI; JAMÁS un techo silencioso del lote.
- Cada job guarda su ``idempotency_key`` ANTES del primer envío; los reintentos
  tras fallos ambiguos (timeout/5xx) reutilizan la misma clave -> el proveedor
  devuelve el mismo ``request_id`` y no se duplica el cargo.
- Un job con ``request_id`` jamás se vuelve a enviar: solo se consulta estado.
- Polling con backoff (2s -> 10s + jitter); 429/5xx/errores de red reintentables.
- Cancelación real en el proveedor (endpoint documentado); detener el polling
  no se considera cancelación.
- Reanudación tras reiniciar la app: jobs con request_id continúan en polling,
  los aceptados-perdidos se recuperan con la misma clave.
- El manifiesto del lote se escribe al finalizar (trazabilidad sin secretos).
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .models import (
    BATCH_FINISHED,
    BATCH_RUNNING,
    GLOBAL_MAX_CONCURRENCY,
    REMOTE_TERMINAL_MAP,
    UI_CONCURRENCY_DEFAULT,
    Batch,
    BatchConfig,
    Job,
    JobStatus,
    Project,
    ValidationError,
    build_generation_payload,
    make_job,
    new_id,
)
from .prompts import compose_final_prompt
from .provider.base import Estimate, ImageProvider, ProviderError
from .storage import Store

# mensaje de error cuando la API remota termina en "nsfw"
_NSFW_ERROR = (
    "Contenido rechazado por moderación (nsfw). Las solicitudes nsfw/fallidas "
    "no se cobran según la documentación de Higgsfield."
)


@dataclass
class RunnerConfig:
    """Ajustes del runner (GEN-15 separa default por lote y techo global).

    ``concurrency``: valor de ARRANQUE (preferencia del entorno, 1-8). Es el
    fallback ante lotes sin concurrencia válida y no limita nunca por debajo
    de lo elegido en la UI.
    ``global_max``: techo de seguridad del PROCESO; dimensiona el pool de
    hilos. Nunca supera GLOBAL_MAX_CONCURRENCY (8), ni tan siquiera configurando
    más, aunque el proyecto lo admita técnica y teóricamente.
    """

    concurrency: int = UI_CONCURRENCY_DEFAULT
    global_max: int = GLOBAL_MAX_CONCURRENCY
    poll_initial: float = 2.0
    poll_max: float = 10.0
    poll_jitter: float = 0.5
    submit_max_attempts: int = 4
    download_max_attempts: int = 3
    status_error_max: int = 10
    backoff_max: float = 30.0


def resolve_reference_urls(
    store: Store,
    provider: ImageProvider,
    project: Project,
    reference_ids: list[str],
) -> list[str]:
    """URLs públicas de las referencias asignadas; sube las que falten.

    La URL firmada de subida nunca se persiste ni se registra: solo la
    ``public_url`` resultante, que es la que viaja en ``image_urls``.
    """
    urls: list[str] = []
    changed = False
    for ref_id in reference_ids:
        reference = project.reference_by_id(ref_id)
        if reference is None:
            raise ValidationError(
                f"La referencia '{ref_id}' ya no existe en el proyecto."
            )
        if not reference.public_url:
            abs_path = store.reference_path(project, reference.path)
            if not abs_path.is_file():
                raise ValidationError(
                    f"Falta el archivo de la referencia «{reference.filename}»."
                )
            reference.public_url = provider.upload_reference(
                abs_path, reference.mime
            )
            reference.uploaded_at_remote = time.time()
            changed = True
        urls.append(reference.public_url)
    if changed:
        store.save_project(project)
    return urls


def _estimate_best_effort(
    provider: ImageProvider, model_id: str, payload: dict[str, Any]
) -> Estimate | None:
    try:
        return provider.estimate(model_id, payload)
    except ProviderError:
        # Coste no disponible no debe bloquear la planificación.
        return None


def plan_batch(
    store: Store,
    provider: ImageProvider,
    project: Project,
    config: BatchConfig,
    regenerates_batch: str | None = None,
) -> tuple[Batch, list[Job]]:
    """Prepara un lote en memoria (sin persistir): payloads, versiones y costes."""
    specs = {spec.id: spec for spec in provider.model_specs()}
    if config.model not in specs:
        raise ValidationError(
            f"El proveedor activo no expone el modelo '{config.model}'."
        )

    batch = Batch(
        id=new_id(),
        project_slug=project.slug,
        config=config,
        regenerates_batch=regenerates_batch,
    )
    if not project.prompts:
        raise ValidationError("El proyecto no tiene prompts para generar.")

    jobs: list[Job] = []
    for entry in project.prompts:
        urls = resolve_reference_urls(store, provider, project, entry.reference_ids)
        resolution = entry.resolution or config.resolution
        ratio = entry.aspect_ratio or config.aspect_ratio
        quality = entry.quality or config.quality
        final_prompt = compose_final_prompt(project.art_direction, entry)

        payload = build_generation_payload(
            specs[config.model],
            final_prompt,
            resolution=resolution,
            aspect_ratio=ratio,
            quality=quality,
            image_urls=urls,
        )
        job = make_job(
            batch_id=batch.id,
            project_slug=project.slug,
            entry=entry,
            version=store.next_version(project.slug, entry.slug),
            model=config.model,
            final_prompt=final_prompt,
            payload=payload,
        )
        estimate = _estimate_best_effort(provider, config.model, payload)
        if estimate is not None:
            job.estimate_credits = estimate.credits
            job.estimate_usd = estimate.usd
        jobs.append(job)
    return batch, jobs


def plan_regeneration(
    store: Store,
    provider: ImageProvider,
    project: Project,
    original_jobs: list[Job],
    config: BatchConfig,
) -> tuple[Batch, list[Job]]:
    """Regeneración selectiva: un lote nuevo con versiones superiores.

    Cada job nuevo referencia al original (``regenerates``) y usa una
    ``idempotency_key`` nueva (es una generación deliberadamente distinta).
    """
    if not original_jobs:
        raise ValidationError("No hay imágenes seleccionadas para regenerar.")

    batch = Batch(
        id=new_id(),
        project_slug=project.slug,
        config=config,
        regenerates_batch=original_jobs[0].batch_id,
    )
    specs = {spec.id: spec for spec in provider.model_specs()}
    if config.model not in specs:
        raise ValidationError(
            f"El proveedor activo no expone el modelo '{config.model}'."
        )

    jobs: list[Job] = []
    for original in original_jobs:
        entry = next(
            (p for p in project.prompts if p.id == original.prompt_id), None
        )
        if entry is None:
            raise ValidationError(
                f"El prompt de «{original.asset_name}» ya no existe en el proyecto; "
                "no se puede regenerar."
            )
        urls = resolve_reference_urls(store, provider, project, entry.reference_ids)
        resolution = entry.resolution or config.resolution
        ratio = entry.aspect_ratio or config.aspect_ratio
        quality = entry.quality or config.quality
        final_prompt = compose_final_prompt(project.art_direction, entry)

        payload = build_generation_payload(
            specs[config.model],
            final_prompt,
            resolution=resolution,
            aspect_ratio=ratio,
            quality=quality,
            image_urls=urls,
        )
        job = make_job(
            batch_id=batch.id,
            project_slug=project.slug,
            entry=entry,
            version=store.next_version(project.slug, entry.slug),
            model=config.model,
            final_prompt=final_prompt,
            payload=payload,
            regenerates=original.id,
        )
        estimate = _estimate_best_effort(provider, config.model, payload)
        if estimate is not None:
            job.estimate_credits = estimate.credits
            job.estimate_usd = estimate.usd
        jobs.append(job)
    return batch, jobs


class BatchRunner:
    """Orquesta lotes: envío, polling, descarga, cancelación y reanudación.

    GEN-15 planifica con una ventana deslizante POR LOTE dentro de un pool
    compartido: cada lote mantiene en vuelo como máximo su concurrencia
    elegida (``BatchConfig.concurrency`` acotada al pool) y el pool nunca
    ejecuta más de ``global_max`` jobs del proceso a la vez.
    """

    def __init__(
        self,
        store: Store,
        provider: ImageProvider,
        config: RunnerConfig | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config or RunnerConfig()
        self._pool_size = max(1, min(self.config.global_max, GLOBAL_MAX_CONCURRENCY))
        self._executor = ThreadPoolExecutor(
            max_workers=self._pool_size,
            thread_name_prefix="visualforge-job",
        )
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._enqueued: set[str] = set()
        # GEN-15: estado por lote de la ventana deslizante.
        self._batch_limits: dict[str, int] = {}
        self._batch_queues: dict[str, deque[str]] = {}
        self._batch_inflight: dict[str, int] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, list[Future[None]]] = {}

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def start_batch(self, batch_id: str) -> int:
        """Encola todos los jobs pendientes del lote. Devuelve cuántos se inician."""
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise ValidationError(f"Lote no encontrado: '{batch_id}'")
        if batch.status == BATCH_FINISHED:
            raise ValidationError("El lote ya finalizó; use la regeneración selectiva.")

        self.store.set_batch_status(batch_id, BATCH_RUNNING)
        jobs = self.store.get_jobs(batch_id)
        with self._lock:
            self._cancel_events.setdefault(batch_id, threading.Event())
            self._batch_limits[batch_id] = self._limit_for(batch)
            queue = self._batch_queues.setdefault(batch_id, deque())
            started = 0
            for job in jobs:
                if job.status != JobStatus.PENDING:
                    continue
                if job.id in self._enqueued or job.id in self._active:
                    continue  # ya en cola de este runner: no duplicar
                queue.append(job.id)
                self._enqueued.add(job.id)
                started += 1
        self._refill(batch_id)

        if started == 0:
            self._maybe_finish_batch(batch_id)
        return started

    def resume(self) -> int:
        """Reanuda jobs activos tras reiniciar la app. Idempotente por proceso."""
        resumed = 0
        to_refill: set[str] = set()
        for job in self.store.active_jobs():
            batch = self.store.get_batch(job.batch_id)
            can_resume = bool(
                job.request_id
                or (batch is not None and batch.status == BATCH_RUNNING)
            )
            if not can_resume:
                continue
            with self._lock:
                self._cancel_events.setdefault(job.batch_id, threading.Event())
                self._batch_limits.setdefault(job.batch_id, self._limit_for(batch))
                if job.id in self._enqueued or job.id in self._active:
                    continue
                self._batch_queues.setdefault(job.batch_id, deque()).append(job.id)
                self._enqueued.add(job.id)
            to_refill.add(job.batch_id)
            resumed += 1
        for batch_id in to_refill:
            self._refill(batch_id)
        return resumed

    def cancel_batch(self, batch_id: str) -> dict[str, int]:
        """Cancela tareas pendientes y solicita cancelación real en el proveedor."""
        with self._lock:
            event = self._cancel_events.get(batch_id)
        if event is not None:
            event.set()

        stats = {"canceled": 0, "cancel_requested": 0, "not_cancelable": 0}
        for job in self.store.get_jobs(batch_id):
            if job.is_terminal:
                continue
            outcome = self.store.try_cancel_unsubmitted(job.id)
            if outcome == "canceled":
                stats["canceled"] += 1
                continue
            if outcome == "terminal":
                continue
            # Ya tiene request_id: cancelación real en el proveedor.
            fresh = self.store.get_job(job.id)
            if fresh is None or fresh.is_terminal or not fresh.request_id:
                continue
            try:
                self.provider.cancel(fresh.request_id)
            except ProviderError as exc:
                # El proveedor rechaza cancelar (p. ej. ya en procesamiento):
                # NO marcar como cancelado: se sigue consultando hasta terminal.
                self.store.update_job(
                    fresh.id, error=f"No se pudo cancelar en el proveedor: {exc}"
                )
                stats["not_cancelable"] += 1
            else:
                self.store.update_job(
                    fresh.id,
                    status=JobStatus.CANCELED,
                    error="Cancelado en el proveedor por el usuario.",
                    finished_at=time.time(),
                )
                stats["cancel_requested"] += 1
        self._maybe_finish_batch(batch_id)
        return stats

    def wait(self, batch_id: str, timeout: float = 60.0) -> bool:
        """Bloquea hasta que terminen los jobs gestionados del lote (tests/UI).

        GEN-15: la ventana deslizante publica futures por tandas; tras esperar
        cada tanda se revisita el registro hasta agotar lo recién lanzado y la
        cola local del lote.
        """
        deadline = time.time() + timeout
        waited = 0
        while True:
            with self._lock:
                futures = tuple(self._futures.get(batch_id, ()))[waited:]
                has_queued = bool(self._batch_queues.get(batch_id))
            for future in futures:
                remaining = max(0.0, deadline - time.time())
                try:
                    future.result(timeout=remaining)
                except TimeoutError:
                    return False
            waited += len(futures)
            if not futures and not has_queued:
                return True

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    # ------------------------------------------------------------------
    # Ciclo de vida de un job
    # ------------------------------------------------------------------

    def _run_job(
        self, job_id: str, batch_id: str, cancel_event: threading.Event
    ) -> None:
        with self._lock:
            if job_id in self._active:
                return  # otro hilo ya posee este job (evita duplicar envíos)
            self._active.add(job_id)
        try:
            job = self.store.get_job(job_id)
            if job is None or job.is_terminal:
                return
            if job.status == JobStatus.PENDING:
                self.store.update_job(job_id, status=JobStatus.QUEUED)

            if job.request_id is None:
                job = self._submit(job, cancel_event)
                if job is None:
                    return
            self._poll_until_terminal(job)
        finally:
            with self._lock:
                self._active.discard(job_id)
                self._enqueued.discard(job_id)
                inflight = self._batch_inflight.get(batch_id, 0)
                if inflight > 0:
                    self._batch_inflight[batch_id] = inflight - 1
            # GEN-15: el hueco liberado lanza el siguiente job de la cola del
            # lote; la capacidad del pool es global para todos los lotes.
            self._refill(batch_id)
            self._maybe_finish_batch(batch_id)

    def _submit(self, job: Job, cancel_event: threading.Event) -> Job | None:
        """Envío con reintentos reutilizando SIEMPRE la misma idempotency_key."""
        attempts = job.attempts
        while True:
            fresh = self.store.get_job(job.id)
            if fresh is None or fresh.is_terminal:
                return None
            if cancel_event.is_set():
                # Cancelación solicitada: no enviar nada nuevo.
                self.store.try_cancel_unsubmitted(job.id)
                fresh = self.store.get_job(job.id)
                if fresh is None or fresh.is_terminal:
                    return None
                if fresh.request_id:
                    # Aceptado antes de cancelar: se mantiene en polling.
                    job.request_id = fresh.request_id
                    return job

            attempts += 1
            try:
                result = self.provider.submit(
                    job.model, job.payload, job.idempotency_key
                )
            except ProviderError as exc:
                self.store.update_job(job.id, attempts=attempts)
                if not exc.retryable:
                    self._fail(job.id, f"Envío rechazado: {exc}")
                    return None
                if attempts >= self.config.submit_max_attempts:
                    self._fail(
                        job.id,
                        f"Sin éxito tras {attempts} intentos de envío: {exc}",
                    )
                    return None
                # Misma clave -> mismo request_id si el 1er envío llegó a aceptarse.
                self._sleep(self._backoff(attempts), cancel_event)
                continue

            accepted = self.store.update_job_if_unsubmitted(
                job.id,
                request_id=result.request_id,
                status=JobStatus.RUNNING,
                attempts=attempts,
                error=None,
            )
            job.request_id = result.request_id
            job.attempts = attempts
            if not accepted:
                # Cancelado durante el envío: la solicitud ya existe en el
                # proveedor -> cancelación real; si no es posible, seguir el ciclo.
                self.store.update_job(
                    job.id, request_id=result.request_id, attempts=attempts
                )
                try:
                    self.provider.cancel(result.request_id)
                except ProviderError as exc:
                    self.store.update_job(
                        job.id,
                        status=JobStatus.RUNNING,
                        error=f"Cancelación no permitida tras el envío: {exc}",
                        attempts=attempts,
                    )
                    return job
                self.store.update_job(
                    job.id,
                    status=JobStatus.CANCELED,
                    error=(
                        "Cancelado durante el envío. Si el proveedor ya había "
                        "iniciado el proceso, el cargo se aplica a su política."
                    ),
                    attempts=attempts,
                    finished_at=time.time(),
                )
                return None
            return job

    def _poll_until_terminal(self, job: Job) -> None:
        # El despertar por cancelación se resuelve leyendo el estado en BD en
        # cada vuelta; aquí se duerme a tiempo real para no provocar bucles
        # intensivos cuando el lote queda cancelado pero el job sigue activo.
        assert job.request_id is not None
        delay = self.config.poll_initial
        errors = 0

        while True:
            fresh = self.store.get_job(job.id)
            if fresh is None or fresh.is_terminal:
                return  # cancelado localmente u observado por otro hilo

            try:
                remote = self.provider.status(job.request_id)
            except ProviderError as exc:
                if not exc.retryable:
                    self._fail(job.id, f"Consulta de estado fallida: {exc}")
                    return
                errors += 1
                if errors > self.config.status_error_max:
                    self._fail(
                        job.id,
                        f"Demasiadas consultas fallidas seguidas: {exc}",
                    )
                    return
                time.sleep(min(delay, self.config.backoff_max))
                delay = min(delay * 1.5, self.config.poll_max)
                continue

            errors = 0
            mapped = REMOTE_TERMINAL_MAP.get(remote.status)
            if mapped is None:
                time.sleep(delay + random.uniform(0, self.config.poll_jitter))
                delay = min(delay * 1.5, self.config.poll_max)
                continue

            if mapped == JobStatus.COMPLETED:
                self._handle_completed(job, remote.image_urls, remote.error)
                return
            if mapped == JobStatus.FAILED:
                message = remote.error or "El proveedor informó 'failed'."
                if remote.status == "nsfw":
                    message = _NSFW_ERROR
                self._fail(job.id, message)
                return
            # canceled en el proveedor
            self.store.update_job(
                job.id,
                status=JobStatus.CANCELED,
                error="Cancelado en el proveedor.",
                finished_at=time.time(),
            )
            return

    def _handle_completed(
        self, job: Job, image_urls: tuple[str, ...], remote_error: str | None
    ) -> None:
        if not image_urls:
            self._fail(job.id, remote_error or "Completado sin URLs de resultado.")
            return
        result_url = image_urls[0]
        self.store.update_job(job.id, result_url=result_url)

        dest_base = (
            self.store.batch_results_dir(job.batch_id, job.project_slug)
            / job.id
            / f"original-v{job.version}"
        )
        last_error: ProviderError | None = None
        for attempt in range(1, self.config.download_max_attempts + 1):
            try:
                written = self.provider.download(result_url, dest_base)
            except ProviderError as exc:
                if not exc.retryable:
                    self._fail(
                        job.id,
                        f"Descarga fallida: {exc} (resultado disponible: "
                        "la URL temporal se conserva en el job).",
                    )
                    return
                last_error = exc
                self._sleep(self._backoff(attempt), threading.Event())
                continue
            project_dir = self.store.project_dir(job.project_slug)
            relative = written.relative_to(project_dir).as_posix()
            self.store.update_job(
                job.id,
                status=JobStatus.COMPLETED,
                original_path=relative,
                error=None,
                finished_at=time.time(),
            )
            return

        self._fail(
            job.id,
            f"Descarga fallida tras {self.config.download_max_attempts} intentos: "
            f"{last_error}",
        )

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def _limit_for(self, batch: Batch | None) -> int:
        """Límite efectivo de concurrencia de un lote (GEN-15).

        Manda la cifra elegida para el lote (``BatchConfig.concurrency``),
        acotada al pool global. Si el lote carece de un valor válido (p. ej.
        datos antiguos o ``<= 0``) se usa la preferencia de arranque
        (``RunnerConfig.concurrency``), nunca un valor heredado superior.
        """
        value = (
            batch.config.concurrency if batch is not None else self.config.concurrency
        )
        if not isinstance(value, int) or value < 1:
            value = self.config.concurrency
        return max(1, min(value, self._pool_size))

    def _refill(self, batch_id: str) -> None:
        """Lanza los siguientes jobs del lote mientras haya ventana disponible.

        Mantendrá en vuelo (enviados al pool y no terminados) como máximo el
        límite del lote; el resto permanece en su cola local. Los jobs que
        quedaron cancelados mientras esperaban se saltan para liberar hueco.
        """
        limit = self._batch_limits.get(batch_id)
        if limit is None:
            return
        while True:
            with self._lock:
                if self._batch_inflight.get(batch_id, 0) >= limit:
                    return
                queue = self._batch_queues.get(batch_id)
                if not queue:
                    return
                job_id = queue.popleft()
                self._batch_inflight[batch_id] = (
                    self._batch_inflight.get(batch_id, 0) + 1
                )
                event = self._cancel_events.get(batch_id)
            job = self.store.get_job(job_id)
            if job is None or job.is_terminal:
                # Cancelado o borrado mientras esperaba: libera el hueco.
                with self._lock:
                    self._enqueued.discard(job_id)
                    self._batch_inflight[batch_id] = max(
                        0, self._batch_inflight[batch_id] - 1
                    )
                continue
            future = self._executor.submit(
                self._run_job,
                job_id,
                batch_id,
                event if event is not None else threading.Event(),
            )
            with self._lock:
                self._futures.setdefault(batch_id, []).append(future)

    def _fail(self, job_id: str, message: str) -> None:
        self.store.update_job(
            job_id,
            status=JobStatus.FAILED,
            error=message,
            finished_at=time.time(),
        )

    def _backoff(self, attempt: int) -> float:
        return min(self.config.backoff_max, 2.0 ** min(attempt, 6))

    def _sleep(self, seconds: float, cancel_event: threading.Event) -> None:
        if seconds > 0:
            cancel_event.wait(seconds)

    def _maybe_finish_batch(self, batch_id: str) -> None:
        jobs = self.store.get_jobs(batch_id)
        if jobs and all(job.is_terminal for job in jobs):
            batch = self.store.get_batch(batch_id)
            if batch is not None and batch.status != BATCH_FINISHED:
                self.store.set_batch_status(
                    batch_id, BATCH_FINISHED, finished_at=time.time()
                )
                self.store.write_batch_manifest(batch, jobs)
