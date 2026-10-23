"""Ejecución de lotes: cola con concurrencia controlada y reintentos seguros.

Garantías (requisitos GEN-11):

- Concurrencia limitada (por defecto 2) mediante ``ThreadPoolExecutor``.
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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .models import (
    BATCH_FINISHED,
    BATCH_RUNNING,
    REMOTE_TERMINAL_MAP,
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
    concurrency: int = 2
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
    """Orquesta lotes: envío, polling, descarga, cancelación y reanudación."""

    def __init__(
        self,
        store: Store,
        provider: ImageProvider,
        config: RunnerConfig | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config or RunnerConfig()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.config.concurrency),
            thread_name_prefix="visualforge-job",
        )
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._enqueued: set[str] = set()
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
        event = threading.Event()
        with self._lock:
            self._cancel_events[batch_id] = event

        started = 0
        futures: list[Future[None]] = []
        for job in self.store.get_jobs(batch_id):
            if job.status != JobStatus.PENDING:
                continue
            with self._lock:
                if job.id in self._enqueued or job.id in self._active:
                    continue  # ya en cola de este runner: no duplicar
                self._enqueued.add(job.id)
            futures.append(self._executor.submit(self._run_job, job.id, batch_id, event))
            started += 1
        with self._lock:
            self._futures.setdefault(batch_id, []).extend(futures)

        if started == 0:
            self._maybe_finish_batch(batch_id)
        return started

    def resume(self) -> int:
        """Reanuda jobs activos tras reiniciar la app. Idempotente por proceso."""
        resumed = 0
        for job in self.store.active_jobs():
            batch = self.store.get_batch(job.batch_id)
            can_resume = bool(
                job.request_id
                or (batch is not None and batch.status == BATCH_RUNNING)
            )
            if not can_resume:
                continue
            event = self._cancel_events.setdefault(job.batch_id, threading.Event())
            with self._lock:
                if job.id in self._active or job.id in self._enqueued:
                    continue
                self._enqueued.add(job.id)
            self._executor.submit(self._run_job, job.id, job.batch_id, event)
            resumed += 1
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
        """Bloquea hasta que terminen los futures de un lote (principalmente tests)."""
        with self._lock:
            futures = list(self._futures.get(batch_id, []))
        deadline = time.time() + timeout
        for future in futures:
            remaining = max(0.0, deadline - time.time())
            try:
                future.result(timeout=remaining)
            except TimeoutError:
                return False
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
