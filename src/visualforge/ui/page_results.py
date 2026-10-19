"""Página «Resultados»: galería antes/después, versiones, regeneración y export."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ..config import Settings
from ..export import ExportOptions, export_batch
from ..jobs import BatchRunner, plan_regeneration
from ..models import Batch, BatchConfig, Job, JobStatus, ValidationError
from ..provider.base import ImageProvider, ProviderError
from ..storage import Store
from .common import current_project, go, job_status_mark, open_folder

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _job_config(store: Store, batch: Batch, settings: Settings) -> BatchConfig:
    """Configuración para regenerar: la sesión del usuario o la del lote."""
    if st.session_state.get("lote_model"):
        return BatchConfig(
            model=st.session_state["lote_model"],
            resolution=st.session_state.get("lote_resolution", "2k"),
            aspect_ratio=st.session_state.get("lote_ratio", "auto"),
            quality=st.session_state.get("lote_quality"),
            concurrency=st.session_state.get("lote_concurrency", settings.concurrency),
        )
    return batch.config


def _reference_preview(store: Store, project, job: Job, col) -> None:
    if not job.reference_ids:
        return
    reference = project.reference_by_id(job.reference_ids[0])
    if reference is None:
        return
    path = store.reference_path(project, reference.path)
    if path.is_file():
        col.image(str(path), use_container_width=True, caption="Referencia")


def _gallery_tab(
    store: Store,
    settings: Settings,
    provider: ImageProvider,
    runner: BatchRunner,
    project,
    batch: Batch,
    jobs: list[Job],
) -> None:
    renderable = [job for job in jobs if job.original_path]
    if not renderable:
        st.info("Este lote todavía no tiene imágenes descargadas.")
    else:
        st.subheader(f"Galería ({len(renderable)} imagen(es))")
        for index in range(0, len(renderable), 3):
            cols = st.columns(3)
            for offset, job in enumerate(renderable[index : index + 3]):
                col = cols[offset]
                with col:
                    st.markdown(f"**{job.asset_name}** · `v{job.version}`")
                    path = store.project_dir(project.slug) / job.original_path
                    if path.is_file():
                        st.image(str(path), use_container_width=True)
                        data = path.read_bytes()
                        st.download_button(
                            f"⬇️ Descargar {path.suffix.lstrip('.') or 'imagen'}",
                            data=data,
                            file_name=path.name,
                            mime=_MIME_BY_SUFFIX.get(path.suffix, "application/octet-stream"),
                            key=f"dl_{job.id}",
                            use_container_width=True,
                        )
                    else:
                        st.warning("Archivo original no encontrado en disco.")
                    st.caption(job_status_mark(job))
                    with st.expander("Referencia (antes)"):
                        _reference_preview(store, project, job, st)

    # Regeneración selectiva
    regenerable = [
        job
        for job in jobs
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}
    ]
    st.subheader("🔁 Regeneración selectiva")
    if not regenerable:
        st.caption("No hay imágenes completadas o fallidas para regenerar.")
    else:
        selected = st.multiselect(
            "Elige las imágenes a regenerar (se crearán como versión nueva, "
            "sin sobrescribir las actuales)",
            options=[job.id for job in regenerable],
            format_func=lambda jid: next(
                f"{job.asset_name} · v{job.version} · {str(job.status)}"
                for job in regenerable
                if job.id == jid
            ),
            key="regen_select",
        )
        if st.button(
            "🔁 Regenerar seleccionadas",
            type="primary",
            disabled=not selected,
        ):
            originals = [job for job in regenerable if job.id in selected]
            config = _job_config(store, batch, settings)
            try:
                with st.spinner("Planificando regeneración…"):
                    new_batch, new_jobs = plan_regeneration(
                        store, provider, project, originals, config
                    )
                    store.create_batch(new_batch, new_jobs)
                    runner.start_batch(new_batch.id)
            except (ValidationError, ProviderError) as exc:
                st.error(f"No se pudo regenerar: {exc}")
            else:
                st.session_state["exec_batch_id"] = new_batch.id
                st.success(f"Regenerando {len(new_jobs)} imagen(es) seleccionadas.")
                st.rerun()


def _history_tab(store: Store, project, jobs: list[Job]) -> None:
    by_slug: dict[str, list[Job]] = {}
    for job in store.list_project_jobs(project.slug):
        by_slug.setdefault(job.slug, []).append(job)

    if not by_slug:
        st.info("Sin historial todavía.")
        return

    for slug in sorted(by_slug):
        versions = sorted(by_slug[slug], key=lambda j: j.version, reverse=True)
        latest = versions[0]
        with st.expander(
            f"🗂️ {latest.asset_name} (`{slug}`) — {len(versions)} versión(es)"
        ):
            st.dataframe(
                [
                    {
                        "Versión": f"v{job.version}",
                        "Estado": str(job.status),
                        "Lote": job.batch_id[:8],
                        "Modelo": job.model,
                        "request_id": (job.request_id or "—")[:14],
                        "Regenera a": (job.regenerates or "—")[:14],
                        "Creado": pd.Timestamp(job.created_at, unit="s").strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        "Error": (job.error or "—")[:100],
                    }
                    for job in versions
                ],
                use_container_width=True,
                hide_index=True,
            )


def _export_tab(store: Store, project, batch: Batch, jobs: list[Job]) -> None:
    st.subheader("📦 Exportar lote")
    col1, col2, col3 = st.columns(3)
    with col1:
        web_format = st.selectbox(
            "Formato web",
            options=["webp", "jpeg", "png"],
            help="WEBP es el recomendado para web (menor peso).",
        )
    with col2:
        quality = st.slider("Calidad", 40, 100, 82)
    with col3:
        max_width = st.selectbox(
            "Ancho máximo (solo reduce)",
            options=["Sin límite", "2560", "1920", "1280"],
        )

    completed = [job for job in jobs if job.original_path]
    if not completed:
        st.info("Este lote no tiene imágenes descargadas para exportar.")
        return

    if st.button("📦 Exportar (originales + web + manifiesto + ZIP)", type="primary"):
        options = ExportOptions(
            web_format=web_format,
            quality=quality,
            max_width=None if max_width == "Sin límite" else int(max_width),
        )
        try:
            with st.spinner("Generando paquete de exportación…"):
                result = export_batch(store, project, batch, jobs, options)
        except (ValidationError, OSError) as exc:
            st.error(f"Exportación fallida: {exc}")
            return
        st.session_state["last_export"] = {
            "zip": str(result.zip_path) if result.zip_path else "",
            "dir": str(result.export_dir),
            "project": project.slug,
        }
        st.rerun()

    last = st.session_state.get("last_export")
    if last and last.get("project") == project.slug:
        c1, c2 = st.columns(2)
        c1.metric("Imágenes exportadas", len(completed))
        c2.metric("Carpeta", last["dir"])
        zip_file = Path(last.get("zip") or "")
        if zip_file.is_file():
            st.download_button(
                "⬇️ Descargar ZIP del lote",
                data=zip_file.read_bytes(),
                file_name=zip_file.name,
                mime="application/zip",
            )
        if st.button("📂 Abrir carpeta de exportación"):
            open_folder(Path(last["dir"]))

    st.caption(
        "El paquete incluye `originals/`, `web/` (optimizados), `prompts.json`, "
        "`manifest.json` y `README-assets.md` con rutas portables. No incluye "
        "claves ni URLs firmadas."
    )


def render(
    store: Store,
    settings: Settings,
    provider: ImageProvider,
    runner: BatchRunner,
) -> None:
    project = current_project(store)
    if project is None:
        st.title("🖼️ Resultados")
        st.warning("Crea o abre un proyecto en «Proyectos» para ver resultados.")
        return

    st.title(f"🖼️ Resultados — {project.name}")
    batches = store.list_batches(project.slug)
    if not batches:
        st.info("Este proyecto aún no tiene lotes. Genera uno en la pestaña «Lote».")
        return

    default_index = 0
    selected = st.session_state.get("exec_batch_id")
    for i, batch in enumerate(batches):
        if batch.id == selected:
            default_index = i
            break
    batch_id = st.selectbox(
        "Lote",
        options=[b.id for b in batches],
        index=default_index,
        format_func=lambda bid: (
            f"{bid[:8]} · "
            f"{pd.Timestamp(store.get_batch(bid).created_at, unit='s').strftime('%d/%m %H:%M')} · "
            f"{'regeneración' if store.get_batch(bid).regenerates_batch else 'lote'}"
        ),
        key="results_batch_select",
    )
    st.session_state["exec_batch_id"] = batch_id

    batch = store.get_batch(batch_id)
    jobs = store.get_jobs(batch_id)
    if batch is None:
        st.warning("Lote no encontrado.")
        return

    if st.button("⬅️ Volver a ejecución"):
        go("Lote")

    tab_gallery, tab_history, tab_export = st.tabs(
        ["🖼️ Galería", "🗂️ Historial de versiones", "📦 Exportar"]
    )
    with tab_gallery:
        _gallery_tab(store, settings, provider, runner, project, batch, jobs)
    with tab_history:
        _history_tab(store, project, jobs)
    with tab_export:
        _export_tab(store, project, batch, jobs)
