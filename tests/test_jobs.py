"""BatchRunner: idempotencia, reintentos, concurrencia, cancelación y reanudación."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from conftest import FLARE_CONFIG, make_project_with_prompts, wait_for
from visualforge.jobs import BatchRunner, RunnerConfig, plan_batch, plan_regeneration
from visualforge.models import (
    BATCH_RUNNING,
    Batch,
    BatchConfig,
    JobStatus,
    Project,
    ValidationError,
)
from visualforge.provider.base import ProviderError
from visualforge.provider.mock import MockProvider
from visualforge.storage import Store


def _all_terminal(store: Store, batch_id: str) -> bool:
    jobs = store.get_jobs(batch_id)
    return bool(jobs) and all(job.is_terminal for job in jobs)


def _make_planned(store: Store, provider: MockProvider, count: int):
    demo = make_project_with_prompts(store, count)
    batch, jobs = plan_batch(store, provider, demo, FLARE_CONFIG)
    store.create_batch(batch, jobs)
    return batch, jobs


@contextmanager
def _runner_with(
    store: Store,
    provider: MockProvider,
    concurrency: int,
    global_max: int = 8,
):
    """Runner rápido con la ``RunnerConfig`` que define cada caso GEN-15.

    ``concurrency`` simula el valor de arranque de ``.env`` (1-8) y
    ``global_max`` el techo de seguridad del proceso. El proveedor SIEMPRE es
    el mock: jamás se lanza una generación real de Higgsfield en tests.
    """
    config = RunnerConfig(
        concurrency=concurrency,
        global_max=global_max,
        poll_initial=0.0,
        poll_max=0.01,
        poll_jitter=0.0,
        backoff_max=0.01,
    )
    instance = BatchRunner(store, provider, config)
    try:
        yield instance
    finally:
        instance.shutdown(wait=False)


def _planned_with(
    store: Store, provider: MockProvider, count: int, concurrency: int
) -> Batch:
    """Lote planificado y persistido con la concurrencia elegida en la 'UI'."""
    demo = make_project_with_prompts(store, count)
    batch, jobs = plan_batch(
        store, provider, demo, replace(FLARE_CONFIG, concurrency=concurrency)
    )
    store.create_batch(batch, jobs)
    return batch


@pytest.fixture
def single(store: Store, provider: MockProvider):
    return _make_planned(store, provider, 1)


def test_lote_completo_descarga_y_manifiesto(
    store: Store,
    provider: MockProvider,
    runner: BatchRunner,
    planned,
    project,
) -> None:
    batch, _ = planned
    started = runner.start_batch(batch.id)
    assert started == 3
    assert runner.wait(batch.id, timeout=30)
    jobs = store.get_jobs(batch.id)
    assert all(job.status == JobStatus.COMPLETED for job in jobs)
    assert all(job.original_path and job.request_id for job in jobs)
    for job in jobs:
        original = store.project_dir(project.slug) / job.original_path
        assert original.is_file()
    reloaded = store.get_batch(batch.id)
    assert reloaded is not None
    assert reloaded.status == "finished"
    assert reloaded.finished_at is not None
    manifest = store.project_dir(project.slug) / "batches" / batch.id / "manifest.json"
    assert manifest.is_file()
    assert len(provider.charged_keys) == 3


def test_concurrency_respeta_limite(
    store: Store, provider: MockProvider, runner: BatchRunner
) -> None:
    provider.scenarios.polls_to_complete = 50
    batch, _ = _make_planned(store, provider, 10)
    assert runner.start_batch(batch.id) == 10
    assert runner.wait(batch.id, timeout=30)
    assert provider.max_in_flight <= 2
    assert _all_terminal(store, batch.id)


def test_submit_reintentos_sin_cargo_duplicado(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.submit_failures = 2
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 3
    assert provider.submit_calls == 3
    assert len(provider.charged_keys) == 1


def test_respuesta_perdida_reutiliza_misma_clave(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.accept_then_lose_response = True
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 2
    assert provider.submit_calls == 2
    assert len(provider.charged_keys) == 1  # un solo cargo pese a 2 envíos


def test_submit_rechazado_falla_sin_reintentar(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.reject_submits = True
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "Envío rechazado" in (job.error or "")
    assert len(provider.charged_keys) == 0
    assert store.get_batch(batch.id).status == "finished"


def test_fallo_terminal_del_proveedor(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.fail_terminal = True
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "Generación fallida (mock)" in (job.error or "")


def test_nsfw_falla_con_mensaje_de_moderacion(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.nsfw_terminal = True
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "nsfw" in (job.error or "").lower()


def test_status_failures_transitorios(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.status_failures = 3
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    assert store.get_job(jobs[0].id).status == JobStatus.COMPLETED


def test_status_failures_exceden_maximo(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.status_failures = 100
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "consultas fallidas" in (job.error or "")


def test_download_reintentos_y_exito(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.download_failures = 2
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.original_path is not None


def test_download_falla_conserva_result_url(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    provider.scenarios.download_failures = 10
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "Descarga fallida" in (job.error or "")
    assert job.result_url is not None  # la URL temporal se conserva


def test_cancelar_sin_enviar_no_cobra(
    store: Store, provider: MockProvider, runner: BatchRunner, planned
) -> None:
    batch, _ = planned
    stats = runner.cancel_batch(batch.id)
    assert stats["canceled"] == 3
    assert provider.submit_calls == 0
    assert len(provider.charged_keys) == 0
    assert store.get_batch(batch.id).status == "finished"


def test_cancelar_en_curso(
    store: Store, provider: MockProvider, runner: BatchRunner, planned
) -> None:
    batch, _ = planned
    provider.scenarios.polls_to_complete = 1_000_000
    runner.start_batch(batch.id)
    assert wait_for(lambda: any(j.request_id for j in store.get_jobs(batch.id)))
    stats = runner.cancel_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    assert _all_terminal(store, batch.id)
    statuses = {j.status for j in store.get_jobs(batch.id)}
    assert statuses == {JobStatus.CANCELED}
    # Los enviados se cancelan en el proveedor; los no enviados, localmente.
    assert stats["cancel_requested"] + stats["canceled"] >= 1
    # Ningún cargo huérfano: solo los jobs con request_id fueron cobrados.
    assert len(provider.charged_keys) == provider.submit_calls


def test_reanudar_tras_reinicio_no_reenvia(
    store: Store, provider: MockProvider, runner: BatchRunner, planned
) -> None:
    batch, jobs = planned
    # Simular proceso anterior: job1 ya aceptado (request_id + RUNNING),
    # job2 sin enviar, lote en running.
    first = jobs[0]
    result = provider.submit(first.model, first.payload, first.idempotency_key)
    store.update_job(first.id, request_id=result.request_id, status=JobStatus.RUNNING)
    store.set_batch_status(batch.id, BATCH_RUNNING)

    resumed = runner.resume()
    assert resumed == 3
    assert wait_for(lambda: _all_terminal(store, batch.id), timeout=30)
    reloaded = store.get_job(first.id)
    assert reloaded is not None
    assert reloaded.status == JobStatus.COMPLETED
    assert reloaded.attempts == 0  # jamás re-enviado: solo polling
    # 1 submit manual + 2 del runner: el job1 no genera segundo envío.
    assert provider.submit_calls == 3
    assert len(provider.charged_keys) == 3


def test_resume_ignora_terminales(store: Store, runner: BatchRunner, completed_batch) -> None:
    assert runner.resume() == 0


def test_start_batch_inexistente(runner: BatchRunner) -> None:
    with pytest.raises(ValidationError, match="no encontrado"):
        runner.start_batch("no-existe")


def test_start_batch_finalizado_rechazado(
    runner: BatchRunner, completed_batch
) -> None:
    batch, _ = completed_batch
    with pytest.raises(ValidationError, match="ya finalizó"):
        runner.start_batch(batch.id)


def test_regeneracion_selectiva(
    store: Store,
    provider: MockProvider,
    runner: BatchRunner,
    completed_batch,
    project,
) -> None:
    batch, jobs = completed_batch
    original = jobs[0]
    new_batch, new_jobs = plan_regeneration(
        store, provider, project, [original], FLARE_CONFIG
    )
    store.create_batch(new_batch, new_jobs)
    regenerated = new_jobs[0]
    assert regenerated.version == original.version + 1
    assert regenerated.regenerates == original.id
    assert regenerated.idempotency_key != original.idempotency_key
    assert regenerated.filename_base == f"{original.slug}-v{regenerated.version}"

    runner.start_batch(new_batch.id)
    assert runner.wait(new_batch.id, timeout=30)
    assert store.get_job(regenerated.id).status == JobStatus.COMPLETED
    history = store.jobs_for_asset(project.slug, original.slug)
    assert [j.version for j in history] == [2, 1]


def test_regeneracion_rechaza_lista_vacia(store: Store, project) -> None:
    with pytest.raises(ValidationError, match="seleccionadas"):
        plan_regeneration(store, MockProvider(), project, [], FLARE_CONFIG)


def test_plan_batch_sin_prompts(store: Store, provider: MockProvider) -> None:
    empty = Project(slug="vacio", name="Vacío")
    store.save_project(empty)
    with pytest.raises(ValidationError, match="prompts"):
        plan_batch(store, provider, empty, FLARE_CONFIG)


def test_plan_batch_modelo_invisible_al_proveedor(
    store: Store, provider: MockProvider, project
) -> None:
    config = BatchConfig(
        model="modelo-que-no-existe",
        resolution="2k",
        aspect_ratio="16:9",
        quality="high",
    )
    with pytest.raises(ValidationError, match="no expone"):
        plan_batch(store, provider, project, config)


def test_start_batch_doble_no_duplica(
    store: Store, provider: MockProvider, runner: BatchRunner, planned
) -> None:
    batch, _ = planned
    assert runner.start_batch(batch.id) == 3
    assert runner.start_batch(batch.id) == 0  # ya no hay PENDING
    assert runner.wait(batch.id, timeout=30)
    assert _all_terminal(store, batch.id)
    assert len(provider.charged_keys) == 3


def test_provider_error_no_reintentable_en_status(
    store: Store, provider: MockProvider, runner: BatchRunner, single
) -> None:
    batch, jobs = single
    original_status = provider.status
    calls = {"n": 0}

    def failing_status(request_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError(
                "request no encontrado", code="not_found", retryable=False
            )
        return original_status(request_id)

    provider.status = failing_status  # type: ignore[method-assign]
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30)
    job = store.get_job(jobs[0].id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "Consulta de estado fallida" in (job.error or "")


# ---------------------------------------------------------------------------
# GEN-15: la concurrencia elegida en la UI manda por lote; techo global 8.
# Todos los tests usan el proveedor mock: nunca una generación facturable.
# ---------------------------------------------------------------------------


def test_gen15_env_2_con_ui_4_alcanza_4_y_no_supera_4(
    store: Store, provider: MockProvider
) -> None:
    """REGRESIÓN .env=2 + UI=4: el máximo efectivo es 4, no 2 por herencia."""
    provider.scenarios.polls_to_complete = 1_000_000  # nada finaliza mientras se mide
    batch = _planned_with(store, provider, count=6, concurrency=4)
    with _runner_with(store, provider, concurrency=2) as runner:  # arranque .env=2
        assert runner.start_batch(batch.id) == 6
        assert wait_for(lambda: provider.max_in_flight >= 4, timeout=10), (
            "con UI=4 deben poder coexistir 4 generaciones (.env=2 no es techo)"
        )
        assert provider.max_in_flight <= 4
        runner.cancel_batch(batch.id)
        assert runner.wait(batch.id, timeout=30)
    assert _all_terminal(store, batch.id)


def test_gen15_env_5_con_ui_2_la_ui_manda(store: Store, provider: MockProvider) -> None:
    """.env=5 + UI=2: el lote corre con 2; el valor de arranque no lo supera."""
    provider.scenarios.polls_to_complete = 1_000_000
    batch = _planned_with(store, provider, count=6, concurrency=2)
    with _runner_with(store, provider, concurrency=5) as runner:  # arranque .env=5
        assert runner.start_batch(batch.id) == 6
        assert wait_for(lambda: provider.max_in_flight >= 2, timeout=10)
        assert provider.max_in_flight <= 2
        runner.cancel_batch(batch.id)
        assert runner.wait(batch.id, timeout=30)
    assert _all_terminal(store, batch.id)


def test_gen15_lote_de_12_con_selector_4_completa_sin_pasarse(
    store: Store, provider: MockProvider
) -> None:
    """12 imágenes con UI=4: hasta 4 simultáneas, 8 en cola, las 12 completadas."""
    batch = _planned_with(store, provider, count=12, concurrency=4)
    with _runner_with(store, provider, concurrency=2) as runner:  # arranque .env=2
        assert runner.start_batch(batch.id) == 12
        assert runner.wait(batch.id, timeout=60)
    jobs = store.get_jobs(batch.id)
    assert len(jobs) == 12
    assert all(job.status == JobStatus.COMPLETED for job in jobs)
    assert all(job.attempts == 1 for job in jobs)  # sin reintentos: cada envío único
    assert provider.max_in_flight <= 4
    # Sin duplicados: una ayuda de cargo por imagen y una clave distinta por job.
    assert len(provider.charged_keys) == 12
    assert len(set(provider.charged_keys)) == 12


def test_gen15_dos_lotes_en_paralelo_no_superan_ocho(
    store: Store, provider: MockProvider
) -> None:
    """Dos lotes activos: la suma de jobs en vuelo nunca supera el techo 8."""
    provider.scenarios.polls_to_complete = 1_000_000
    batch_a = _planned_with(store, provider, count=6, concurrency=4)
    batch_b = _planned_with(store, provider, count=6, concurrency=4)
    with _runner_with(store, provider, concurrency=2) as runner:
        assert runner.start_batch(batch_a.id) == 6
        assert runner.start_batch(batch_b.id) == 6
        assert wait_for(lambda: provider.max_in_flight >= 8, timeout=10), (
            "ambos lotes deben compaginar capacidad (4+4) sin bloquearse entre sí"
        )
        assert provider.max_in_flight <= 8
        runner.cancel_batch(batch_a.id)
        runner.cancel_batch(batch_b.id)
        assert runner.wait(batch_a.id, timeout=30)
        assert runner.wait(batch_b.id, timeout=30)
    assert _all_terminal(store, batch_a.id)
    assert _all_terminal(store, batch_b.id)


def test_gen15_configuracion_invalida_cae_en_el_valor_de_arranque(
    store: Store, provider: MockProvider
) -> None:
    """BatchConfig sin concurrencia válida (<=0): fallback al arranque (.env)."""
    provider.scenarios.polls_to_complete = 1_000_000
    batch = _planned_with(store, provider, count=6, concurrency=0)
    with _runner_with(store, provider, concurrency=3) as runner:
        runner.start_batch(batch.id)
        assert wait_for(lambda: provider.max_in_flight >= 3, timeout=10)
        assert provider.max_in_flight <= 3
        runner.cancel_batch(batch.id)
        assert runner.wait(batch.id, timeout=30)
    assert _all_terminal(store, batch.id)


def test_gen15_techo_global_configurable_por_debajo_de_8(
    store: Store, provider: MockProvider
) -> None:
    """Dos lotes con límite 3 cada uno y techo global 2: nunca más de 2 en vuelo."""
    provider.scenarios.polls_to_complete = 1_000_000
    batch_a = _planned_with(store, provider, count=6, concurrency=3)
    batch_b = _planned_with(store, provider, count=6, concurrency=3)
    with _runner_with(store, provider, concurrency=3, global_max=2) as runner:
        runner.start_batch(batch_a.id)
        runner.start_batch(batch_b.id)
        assert wait_for(lambda: provider.max_in_flight >= 2, timeout=10)
        assert provider.max_in_flight <= 2
        runner.cancel_batch(batch_a.id)
        runner.cancel_batch(batch_b.id)
        assert runner.wait(batch_a.id, timeout=30)
        assert runner.wait(batch_b.id, timeout=30)
    assert _all_terminal(store, batch_a.id)
    assert _all_terminal(store, batch_b.id)
