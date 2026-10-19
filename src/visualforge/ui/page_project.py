"""Página «Proyecto»: galería de referencias y editor de prompts."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ..config import Settings
from ..models import (
    DEFAULT_MODEL_ID,
    ModelSpec,
    Project,
    PromptEntry,
    ValidationError,
    get_model_spec,
    new_id,
)
from ..prompts import ensure_unique_slug, parse_prompts_text, slugify
from ..storage import Store
from .common import current_project, fmt_bytes

GLOBAL_OPTION = "Usar global"
_UPLOADER_KEY = "project_reference_uploader"


def _clear_uploader() -> None:
    st.session_state[_UPLOADER_KEY] = None


# ---------------------------------------------------------------------------
# Referencias
# ---------------------------------------------------------------------------


def _references_tab(store: Store, project: Project, settings: Settings) -> None:
    st.markdown(
        "Sube **fotografías originales del negocio** (interior, fachada, producto…). "
        "Se usarán como referencia de cada prompt. Solo imágenes con derechos de uso: "
        "nunca se suben a GitHub."
    )
    st.caption(
        f"Formatos: PNG, JPEG, WEBP, GIF · Máx. {settings.max_reference_mb} MB por archivo "
        "(límite local; la API documenta MIME, no un tamaño máximo)."
    )

    uploads = st.file_uploader(
        "Seleccionar fotografías",
        type=["png", "jpg", "jpeg", "webp", "gif"],
        accept_multiple_files=True,
        key=_UPLOADER_KEY,
    )
    if uploads and st.button("⬆️ Subir referencias al proyecto", type="primary"):
        added = 0
        for upload in uploads:
            try:
                store.add_reference(
                    project, upload.name, upload.getvalue(), upload.type or ""
                )
                added += 1
            except ValidationError as exc:
                st.error(f"«{upload.name}»: {exc}")
        if added:
            st.success(f"{added} referencia(s) añadida(s).")
            st.session_state["prompts_editor_reset"] = True
            _clear_uploader()
            st.rerun()

    references = project.references
    if not references:
        st.info("Aún no hay referencias en este proyecto.")
        return

    usage = {
        ref.id: sum(1 for p in project.prompts if ref.id in p.reference_ids)
        for ref in references
    }
    cols = st.columns(4)
    for index, reference in enumerate(references):
        col = cols[index % 4]
        with col:
            path = store.reference_path(project, reference.path)
            if path.is_file():
                st.image(str(path), use_container_width=True, caption=reference.filename)
            else:
                st.warning(f"Falta {reference.filename}")
            st.caption(
                f"{fmt_bytes(reference.size_bytes)} · `{reference.id[:8]}` · "
                f"usado en {usage[reference.id]} prompt(s)"
            )
        if index % 4 == 3 and index < len(references) - 1:
            cols = st.columns(4)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _spec_for_editor() -> ModelSpec:
    model_id = st.session_state.get("lote_model", DEFAULT_MODEL_ID)
    try:
        return get_model_spec(model_id)
    except ValidationError:
        return get_model_spec(DEFAULT_MODEL_ID)


def _rows_from_entries(entries: list[PromptEntry]) -> list[dict]:
    return [
        {
            "ID": entry.id[:8],
            "Nombre": entry.name,
            "Prompt": entry.prompt,
            "Mantener": entry.keep,
            "Ratio": entry.aspect_ratio or GLOBAL_OPTION,
            "Resolución": entry.resolution or GLOBAL_OPTION,
            "Calidad": entry.quality or GLOBAL_OPTION,
        }
        for entry in entries
    ]


def _opt(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text == GLOBAL_OPTION:
        return None
    return text


def _prompts_tab(store: Store, project: Project) -> None:
    spec = _spec_for_editor()
    st.caption(
        f"Opciones por imagen según el modelo activo **{spec.label}** "
        "(cámbialo en la pestaña «Lote»). El identificador de archivo no cambia al "
        "renombrar un asset, para conservar su historial de versiones."
    )

    brief = project.brief
    art_direction = project.art_direction
    with st.expander("🧭 Brief y dirección de arte", expanded=False):
        brief = st.text_area(
            "Brief del proyecto", value=project.brief, height=80, key="project_brief"
        )
        art_direction = st.text_area(
            "Prompt base (se antepone a cada prompt)",
            value=project.art_direction,
            height=120,
            key="project_art",
            help="Paleta, iluminación y estilo compartidos por todo el lote.",
        )

    with st.expander("📥 Importar prompts (uno por línea)"):
        st.caption(
            "Con `::` para nombre: `Hero :: gran foto de portada con terraza al "
            "atardecer`. Sin `::` se genera nombre automático. Se añaden al final."
        )
        raw = st.text_area(
            "Prompts",
            height=150,
            placeholder="Hero :: gran foto de portada con la terraza al atardecer\n"
            "Fachada :: frontal del local con cartel iluminado",
            key="prompts_import_area",
        )
        if st.button("Añadir prompts importados"):
            try:
                new_entries = parse_prompts_text(raw)
            except ValidationError as exc:
                st.error(exc)
            else:
                used = {e.slug for e in project.prompts}
                for entry in new_entries:
                    entry.slug = ensure_unique_slug(entry.slug, used)
                    used.add(entry.slug)
                    project.prompts.append(entry)
                store.save_project(project)
                st.session_state["prompts_editor_reset"] = True
                st.success(f"{len(new_entries)} prompt(s) añadido(s).")
                st.rerun()

    st.subheader(f"Prompts del proyecto ({len(project.prompts)})")
    if not project.prompts:
        st.info("Añade prompts con el importador de arriba o con «+ Fila» de la tabla.")

    if st.session_state.pop("prompts_editor_reset", False):
        st.session_state.pop("prompts_editor", None)

    edited_df = st.data_editor(
        pd.DataFrame(_rows_from_entries(project.prompts)),
        num_rows="dynamic",
        use_container_width=True,
        key="prompts_editor",
        hide_index=True,
        column_config={
            "ID": st.column_config.TextColumn("ID", disabled=True, width="tiny"),
            "Nombre": st.column_config.TextColumn("Nombre", required=True, width="medium"),
            "Prompt": st.column_config.TextColumn("Prompt", width="large", required=True),
            "Mantener": st.column_config.TextColumn(
                "Mantener",
                help="Elementos que no deben cambiar (paleta, mobiliario, lema…).",
                width="medium",
            ),
            "Ratio": st.column_config.SelectboxColumn(
                "Ratio", options=[GLOBAL_OPTION, *spec.aspect_ratios]
            ),
            "Resolución": st.column_config.SelectboxColumn(
                "Resolución", options=[GLOBAL_OPTION, *spec.resolutions]
            ),
            "Calidad": st.column_config.SelectboxColumn(
                "Calidad",
                options=[GLOBAL_OPTION, *(spec.qualities or [])],
                disabled=spec.qualities is None,
                help=(
                    "El modelo activo documenta `quality`."
                    if spec.qualities
                    else f"{spec.label} no documenta `quality`: columna desactivada."
                ),
            ),
        },
    )

    rows = [] if edited_df is None else edited_df.to_dict("records")
    existing_by_short = {entry.id[:8]: entry for entry in project.prompts}
    selected_by_row: dict[int, list[str]] = {}

    if project.references and rows:
        st.subheader("Referencias por prompt")
        st.caption("Asigna las fotos que debe usar cada generación (puedes repetir fotos).")
        ref_options = {r.id: f"{r.filename} ({r.id[:6]})" for r in project.references}
        for position, row in enumerate(rows):
            entry = existing_by_short.get(str(row.get("ID") or ""))
            label_name = str(row.get("Nombre") or "").strip() or f"fila {position + 1}"
            default_ids = (
                [r for r in entry.reference_ids if r in ref_options] if entry else []
            )
            key = f"refs_{entry.id}" if entry else f"refs_row_{position}"
            selected_by_row[position] = st.multiselect(
                f"Referencias de «{label_name}»",
                options=list(ref_options),
                default=default_ids,
                format_func=lambda ref_id: ref_options[ref_id],
                key=key,
            )

    save_clicked = st.button(
        "💾 Guardar prompts, referencias y dirección de arte", type="primary"
    )
    if save_clicked:
        _save_prompts(
            store, project, edited_df, selected_by_row, brief, art_direction
        )


def _save_prompts(
    store: Store,
    project: Project,
    edited_df,
    selected_by_row: dict[int, list[str]],
    brief: str,
    art_direction: str,
) -> None:
    if edited_df is None:
        st.error("No hay filas que guardar.")
        return

    existing_by_short = {entry.id[:8]: entry for entry in project.prompts}
    used_slugs = {entry.slug for entry in project.prompts}
    new_entries: list[PromptEntry] = []
    errors: list[str] = []

    for position, row in enumerate(edited_df.to_dict("records")):
        name = str(row.get("Nombre") or "").strip()
        prompt = str(row.get("Prompt") or "").strip()
        keep = str(row.get("Mantener") or "").strip()
        if not name:
            errors.append(f"Fila {position + 1}: falta el nombre.")
            continue
        if not prompt:
            errors.append(f"Fila {position + 1} («{name}»): el prompt está vacío.")
            continue

        existing = existing_by_short.get(str(row.get("ID") or ""))
        if existing is not None:
            entry = existing
            used_slugs.discard(entry.slug)
            entry.name = name
            entry.prompt = prompt
            entry.keep = keep
            entry.aspect_ratio = _opt(row.get("Ratio"))
            entry.resolution = _opt(row.get("Resolución"))
            entry.quality = _opt(row.get("Calidad"))
            if position in selected_by_row:
                entry.reference_ids = list(selected_by_row[position])
        else:
            slug = ensure_unique_slug(slugify(name), used_slugs)
            used_slugs.add(slug)
            entry = PromptEntry(
                id=new_id(),
                name=name,
                slug=slug,
                prompt=prompt,
                keep=keep,
                resolution=_opt(row.get("Resolución")),
                aspect_ratio=_opt(row.get("Ratio")),
                quality=_opt(row.get("Calidad")),
                reference_ids=list(selected_by_row.get(position, [])),
            )
        new_entries.append(entry)

    if errors:
        for error in errors:
            st.error(error)
        return
    if not new_entries:
        st.error("No hay prompts válidos que guardar.")
        return

    project.brief = str(brief or "").strip()
    project.art_direction = str(art_direction or "").strip()
    project.prompts = new_entries
    store.save_project(project)
    st.session_state["prompts_editor_reset"] = True
    st.success("Proyecto guardado.")
    st.rerun()


def render(store: Store, settings: Settings) -> None:
    project = current_project(store)
    if project is None:
        st.title("🎨 Proyecto")
        st.warning("Crea o abre un proyecto en «Proyectos» para continuar.")
        return

    st.title(f"🎨 {project.name}")
    created = pd.Timestamp(project.created_at, unit="s").strftime("%Y-%m-%d %H:%M")
    st.caption(f"Identificador: `{project.slug}` · Creado: {created}")

    tab_refs, tab_prompts = st.tabs(["🖼️ Referencias", "📝 Prompts"])
    with tab_refs:
        _references_tab(store, project, settings)
    with tab_prompts:
        _prompts_tab(store, project)
