"""Página «Proyectos»: listado y creación de proyectos visuales."""

from __future__ import annotations

import re

import streamlit as st

from ..models import Project
from ..prompts import slugify
from ..storage import Store
from .common import fmt_bytes, go


def _render_create(store: Store) -> None:
    with st.expander("➕ Nuevo proyecto", expanded=False), st.form(
        "create_project", clear_on_submit=False
    ):
            name = st.text_input("Nombre del proyecto", placeholder="Restaurante La Parra")
            brief = st.text_area(
                "Brief / descripción",
                placeholder="Web de un restaurante de barrio con terraza…",
                height=100,
            )
            art_direction = st.text_area(
                "Prompt base de dirección de arte",
                placeholder=(
                    "Paleta cálida en madera y terracota, luz natural suave, "
                    "fotografía editorial, sin personas identificables…"
                ),
                height=100,
                help=(
                    "Se antepone a cada prompt para mantener una dirección artística "
                    "coherente en todo el lote."
                ),
            )
            submitted = st.form_submit_button("Crear proyecto")
            if not submitted:
                return
            if not name.strip():
                st.error("El nombre es obligatorio.")
                return
            base_slug = slugify(name)
            if re.fullmatch(r"[a-z0-9-]+", base_slug) is None:
                st.error("El nombre debe generar un identificador válido (letras/números).")
                return
            if store.project_dir(base_slug).exists():
                st.error(f"Ya existe un proyecto con el identificador «{base_slug}».")
                return
            project = Project(
                slug=base_slug,
                name=name.strip(),
                brief=brief.strip(),
                art_direction=art_direction.strip(),
            )
            store.save_project(project)
            st.success(f"Proyecto «{project.name}» creado.")
            go("Proyecto", project_slug=project.slug)


def render(store: Store) -> None:
    st.title("📁 Proyectos")
    st.caption(
        "Cada proyecto agrupa referencias reales, prompts y sus lotes de generación. "
        "Los archivos se guardan en `projects/` (fuera de git)."
    )

    _render_create(store)

    projects = store.list_projects()
    if not projects:
        st.info("Aún no hay proyectos. Crea el primero arriba.")
        return

    st.subheader("Proyectos existentes")
    for project in projects:
        refs_bytes = sum(r.size_bytes for r in project.references)
        active = project.slug == st.session_state.get("current_project_slug")
        cols = st.columns([3, 1, 1, 1, 1])
        cols[0].markdown(
            f"**{project.name}**  \n`{project.slug}`" + (" · ✅ abierto" if active else "")
        )
        cols[1].metric("Prompts", len(project.prompts))
        cols[2].metric("Referencias", len(project.references))
        cols[3].caption(f"{fmt_bytes(refs_bytes) if refs_bytes else '0 KB'} referenciados")
        cols[4].button(
            "Abrir",
            key=f"open_{project.slug}",
            on_click=go,
            args=("Proyecto", project.slug),
        )
        st.divider()
