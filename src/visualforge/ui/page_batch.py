"""Página «Lote»: configuración, previsualización con coste y ejecución."""

from __future__ import annotations

import sqlite3
import time

import pandas as pd
import streamlit as st

from ..config import PROVIDER_MOCK, Settings
from ..jobs import BatchRunner, plan_batch, plan_regeneration
from ..models import (
    DEFAULT_MODEL_ID,
    Batch,
    BatchConfig,
    Job,
    JobStatus,
    ValidationError,
    get_model_spec,
)
from ..provider.base import ImageProvider, ProviderError
from ..storage import Store
from .common import (
    current_project,
    estimate_total,
    fmt_usd,
    go,
    job_status_mark,
    open_folder,
)

_EXEC_BATCH_KEY = "exec_batch_id"
_PLAN_KEY = "lote_plan"


def _clear_plan() -> None:
    st.session_state.pop(_PLAN_KEY, None)


# ---------------------------------------------------------------------------
# Configuración y previsualización
# ---------------------------------------------------------------------------


def _config_tab(
    store: Store,
    settings: Settings,
    provider: ImageProvider,
    runner: BatchRunner,
    project,
) -> None:
    specs = provider.model_specs()

    col1, col2, col3, col4 = st.columns([3, 2, 2, 2])
    with col1:
        model_id = st.selectbox(
            "Modelo",
            options=[s.id for s in specs],
            index=next(
                (i for i, s in enumerate(specs) if s.id == DEFAULT_MODEL_ID), 0
            ),
            format_func=lambda mid: next(
                (f"{s.label} — {s.description}" for s in specs if s.id == mid), mid
            ),
            key="lote_model",
            help="Solo modelos documentados por la API de Higgsfield.",
        )
    spec = get_model_spec(model_id)
    with col2:
        resolution = st.selectbox(
            "Resolución",
            options=list(spec.resolutions),
            index=list(spec.resolutions).index("2k")
            if "2k" in spec.resolutions
            else 0,
            key="lote_resolution",
        )
    with col3:
        ratio = st.selectbox(
            "Relación de aspecto",
            options=list(spec.aspect_ratios),
            index=list(spec.aspect_ratios).index("auto"),
            key="lote_ratio",
            help="Plantillas web típicas: hero 16:9, tarjetas 4:3 o 3:2, detalle 1:1.",
        )
    with col4:
        if spec.qualities:
            quality = st.selectbox(
                "Calidad",
                options=list(spec.qualities),
                index=list(spec.qualities).index("high")
                if "high" in spec.qualities
                else 0,
                key="lote_quality",
            )
        else:
            quality = None
            st.text_input("Calidad", value="—", disabled=True, help=f"{spec.label} no documenta `quality`.")

    concurrency = st.slider(
        "Concurrencia (solicitudes simultáneas)",
        min_value=1,
        max_value=4,
        value=settings.concurrency,
        key="lote_concurrency",
        help="Conservador por defecto para respetar los límites de la API.",
    )

    if st.button("🔍 Previsualizar lote", type="primary"):
        config = BatchConfig(
            model=model_id,
            resolution=resolution,
            aspect_ratio=ratio,
            quality=quality,
            concurrency=concurrency,
        )
        try:
            with st.spinner(
                "Preparando payloads y subiendo referencias pendientes al proveedor…"
            ):
                batch, jobs = plan_batch(store, provider, project, config)
        except (ValidationError, ProviderError) as exc:
            st.error(f"No se pudo planificar el lote: {exc}")
            _clear_plan()
        else:
            st.session_state[_PLAN_KEY] = {
                "batch": batch,
                "jobs": jobs,
                "created_at": time.time(),
            }
            st.rerun()

    plan = st.session_state.get(_PLAN_KEY)
    if not plan:
        st.info(
            "Configura el lote y pulsa «Previsualizar lote» para ver tareas, prompts "
            "finales, referencias y coste estimado antes de enviar nada."
        )
        return

    _render_plan(store, project, provider, runner, plan)


def _render_plan(
    store: Store, project, provider: ImageProvider, runner: BatchRunner, plan: dict
) -> None:
    batch: Batch = plan["batch"]
    jobs: list[Job] = plan["jobs"]
    is_mock = provider.name == PROVIDER_MOCK

    if len(jobs) != len(project.prompts):
        st.warning(
            "El proyecto ha cambiado desde la previsualización: vuelve a pulsar "
            "«Previsualizar lote»."
        )

    st.subheader(f"Previsualización: {len(jobs)} tarea(s)")

    rows = []
    for job in jobs:
        next((p for p in project.prompts if p.id == job.prompt_id), None)
        refs = len(job.reference_ids)
        rows.append(
            {
                "Asset": job.asset_name,
                "Prompt final": job.final_prompt,
                "Refs": refs,
                "Resolución": job.payload.get("resolution"),
                "Ratio": job.payload.get("aspect_ratio"),
                "Calidad": job.payload.get("quality", "—"),
                "Coste est.": fmt_usd(job.estimate_usd),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("Ver prompts finales completos (exactamente lo que se envía)"):
        for job in jobs:
            st.markdown(f"**{job.asset_name}** (`{job.filename_base}`)")
            st.code(job.final_prompt, language=None)

    total, complete = estimate_total(jobs)
    if is_mock:
        st.info(
            "🧪 **Modo simulación**: no se enviará nada a Higgsfield ni se consumirán "
            "créditos (estimaciones de ejemplo)."
        )
    elif total and complete:
        st.success(
            f"**Coste estimado del lote: {total}** (estimación de la API de Higgsfield; "
            "el cargo final se reconcilia al completar cada solicitud)."
        )
    else:
        st.warning(
            "**Coste exacto no disponible**: la API no devolvió estimación para todas "
            "las tareas. El lote consumirá créditos reales de tu cuenta."
        )

    st.caption(
        "Las imágenes generadas son una *interpretación* del prompt con las "
        "referencias: preservan la identidad visual en lo razonable, pero no se "
        "garantiza reproducción arquitectónica exacta del local."
    )

    needs_confirmation = not is_mock
    confirmed = True
    if needs_confirmation:
        confirmed = st.checkbox(
            f"Entiendo que se enviarán {len(jobs)} solicitudes facturables a "
            "Higgsfield con mi cuenta y puedo cancelar las pendientes.",
            key="lote_confirm",
        )

    col_gen, col_clear = st.columns([2, 1])
    with col_gen:
        generate = st.button(
            "🚀 Generar lote",
            type="primary",
            disabled=not confirmed,
            use_container_width=True,
        )
    with col_clear:
        st.button(
            "Descartar previsualización",
            on_click=_clear_plan,
            use_container_width=True,
        )

    if generate:
        try:
            store.create_batch(batch, jobs)
            started = runner.start_batch(batch.id)
        except sqlite3.IntegrityError:
            st.error(
                "El proyecto cambió desde la previsualización (versiones ya usadas). "
                "Vuelve a pulsar «Previsualizar lote»."
            )
            _clear_plan()
            return
        except (ValidationError, ProviderError, ValueError) as exc:
            st.error(f"No se pudo iniciar el lote: {exc}")
            return
        _clear_plan()
        st.session_state[_EXEC_BATCH_KEY] = batch.id
        st.session_state["nav"] = "Lote"
        st.success(f"Lote iniciado con {started} tarea(s).")
        st.rerun()


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------


def _job_table(jobs: list[Job]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Asset": job.asset_name,
                "Versión": f"v{job.version}",
                "Estado": job_status_mark(job),
                "Intentos": job.attempts,
                "request_id": (job.request_id or "—")[:14],
                "Coste est.": fmt_usd(job.estimate_usd),
                "Error": (job.error or "")[:120] or "—",
            }
            for job in jobs
        ]
    )


def _batch_label(store: Store, batch: Batch) -> str:
    created = pd.Timestamp(batch.created_at, unit="s").strftime("%d/%m %H:%M")
    status = {"pending": "pendiente", "running": "en curso", "finished": "finalizado"}.get(
        batch.status, batch.status
    )
    kind = "regeneración" if batch.regenerates_batch else "lote"
    return f"{kind} {batch.id[:8]} · {created} · {status}"


@st.fragment(run_every="1.5s")
def _batch_progress(store: Store, batch_id: str) -> None:
    batch = store.get_batch(batch_id)
    if batch is None:
        st.warning("Lote no encontrado.")
        return
    jobs = store.get_jobs(batch_id)
    if not jobs:
        st.info("Este lote no tiene tareas.")
        return

    total = len(jobs)
    completed = sum(1 for job in jobs if job.status == JobStatus.COMPLETED)
    failed = sum(1 for job in jobs if job.status == JobStatus.FAILED)
    canceled = sum(1 for job in jobs if job.status == JobStatus.CANCELED)
    active = total - completed - failed - canceled

    st.progress(completed / total, text=f"{completed}/{total} completadas")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Completadas", completed)
    c2.metric("⚙️ Activas", active)
    c3.metric("❌ Fallidas", failed)
    c4.metric("🚫 Canceladas", canceled)

    st.dataframe(_job_table(jobs), use_container_width=True, hide_index=True)

    with st.expander("Detalle de errores"):
        errors = [job for job in jobs if job.error]
        if not errors:
            st.caption("Sin errores en este lote.")
        for job in errors:
            st.markdown(f"**{job.asset_name}** (v{job.version})")
            st.code(job.error, language=None)

    if all(job.is_terminal for job in jobs):
        st.success("Lote finalizado. Revisa resultados y exporta cuando quieras.")


def _execution_tab(
    store: Store,
    provider: ImageProvider,
    runner: BatchRunner,
    project,
) -> None:
    batches = store.list_batches(project.slug)
    if not batches:
        st.info(
            "Aún no hay lotes. Ve a «Configurar y previsualizar» para crear el primero."
        )
        return

    resumed = st.session_state.pop("vf_resumed", None)
    if resumed:
        st.info(f"🔄 Se han reanudado {resumed} tarea(s) activas tras un reinicio.")

    selected = st.session_state.get(_EXEC_BATCH_KEY)
    index = next(
        (i for i, b in enumerate(batches) if b.id == selected),
        0,
    )
    batch_id = st.selectbox(
        "Lote a ver",
        options=[b.id for b in batches],
        index=index,
        format_func=lambda bid: _batch_label(store, store.get_batch(bid)),
        key="exec_batch_select",
    )
    st.session_state[_EXEC_BATCH_KEY] = batch_id

    _batch_progress(store, batch_id)

    batch = store.get_batch(batch_id)
    jobs = store.get_jobs(batch_id)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("❌ Cancelar tareas pendientes", use_container_width=True):
            stats = runner.cancel_batch(batch_id)
            st.toast(
                f"Canceladas: {stats['canceled']} · en proveedor: "
                f"{stats['cancel_requested']} · no cancelables: {stats['not_cancelable']}"
            )
            st.rerun()
    with col2:
        failed = [job for job in jobs if job.status == JobStatus.FAILED]
        if st.button(
            "🔁 Regenerar solo fallidas",
            use_container_width=True,
            disabled=not failed,
        ):
            _start_regeneration(store, provider, runner, project, batch, failed)
    with col3:
        st.button(
            "➡️ Ver resultados",
            use_container_width=True,
            on_click=go,
            args=("Resultados",),
        )
    with col4:
        export_dir = store.project_dir(project.slug) / "exports"
        if st.button("📂 Abrir carpeta", use_container_width=True):
            open_folder(export_dir)

    if batch is not None:
        st.caption(
            f"Modelo `{batch.config.model}` · {batch.config.resolution} · "
            f"{batch.config.aspect_ratio} · concurrencia {batch.config.concurrency}"
        )


def _start_regeneration(
    store: Store,
    provider: ImageProvider,
    runner: BatchRunner,
    project,
    original_batch: Batch | None,
    jobs: list[Job],
) -> None:
    config = None
    if st.session_state.get("lote_model"):
        spec_model = st.session_state["lote_model"]
        config = BatchConfig(
            model=spec_model,
            resolution=st.session_state.get("lote_resolution", "2k"),
            aspect_ratio=st.session_state.get("lote_ratio", "auto"),
            quality=st.session_state.get("lote_quality"),
            concurrency=st.session_state.get("lote_concurrency", 2),
        )
    elif original_batch is not None:
        config = original_batch.config
    if config is None:
        st.error("No hay configuración de lote disponible.")
        return
    try:
        with st.spinner("Planificando la regeneración…"):
            batch, new_jobs = plan_regeneration(store, provider, project, jobs, config)
            store.create_batch(batch, new_jobs)
            runner.start_batch(batch.id)
    except (ValidationError, ProviderError) as exc:
        st.error(f"No se pudo regenerar: {exc}")
        return
    st.session_state[_EXEC_BATCH_KEY] = batch.id
    st.success(f"Regenerando {len(new_jobs)} imagen(es) seleccionadas.")
    st.rerun()


# ---------------------------------------------------------------------------
# Entrada de la página
# ---------------------------------------------------------------------------


def render(
    store: Store,
    settings: Settings,
    provider: ImageProvider,
    runner: BatchRunner,
) -> None:
    project = current_project(store)
    if project is None:
        st.title("⚙️ Lote")
        st.warning("Crea o abre un proyecto en «Proyectos» para configurar un lote.")
        return

    st.title(f"⚙️ Lote — {project.name}")
    tab_cfg, tab_exec = st.tabs(["🛠️ Configurar y previsualizar", "🚀 Ejecución"])
    with tab_cfg:
        _config_tab(store, settings, provider, runner, project)
    with tab_exec:
        _execution_tab(store, provider, runner, project)
