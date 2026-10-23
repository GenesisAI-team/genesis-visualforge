"""Router de la UI: navegación lateral y despacho de páginas."""

from __future__ import annotations

import streamlit as st

from . import page_batch, page_project, page_projects, page_results
from .common import (
    api_key_panel,
    get_provider,
    get_runner,
    get_settings,
    get_store,
    provider_badge,
)

PAGES: tuple[str, ...] = ("Proyectos", "Proyecto", "Lote", "Resultados")
PAGE_ICONS: dict[str, str] = {
    "Proyectos": "📁",
    "Proyecto": "🎨",
    "Lote": "⚙️",
    "Resultados": "🖼️",
}

_CUSTOM_CSS = """
<style>
    .stApp [data-testid="stHeader"] { background: transparent; }
    div.block-container { padding-top: 1.2rem; }
    h1, h2, h3 { letter-spacing: -0.01em; }
    [data-testid="stMetricValue"] { font-size: 1.4rem; }
</style>
"""


def _set_nav(page_name: str) -> None:
    st.session_state["nav"] = page_name


def _sidebar(settings, store) -> None:
    st.markdown("## 🎨 Genesis VisualForge")
    st.caption("Generador local de imágenes por lotes · Linear GEN-11")

    current = st.session_state.get("nav", "Proyectos")
    if current not in PAGES:
        current = "Proyectos"
        st.session_state["nav"] = current

    st.markdown("---")
    for page_name in PAGES:
        is_current = page_name == current
        st.button(
            f"{PAGE_ICONS[page_name]} {page_name}",
            key=f"navbtn_{page_name}",
            type="primary" if is_current else "secondary",
            disabled=is_current,
            use_container_width=True,
            on_click=_set_nav,
            args=(page_name,),
        )
    st.markdown("---")

    provider_badge(settings)
    api_key_panel(settings)

    active_slug = st.session_state.get("current_project_slug")
    if active_slug:
        st.caption(f"Proyecto activo: **{active_slug}**")
    else:
        st.caption("Sin proyecto abierto.")

    st.divider()
    st.caption(
        "Protocolo Ultimate Coding · [Issue GEN-11]"
        "(https://linear.app/genesisnavarra/issue/GEN-11)"
    )


def router() -> None:
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)
    settings = get_settings()
    store = get_store(settings)

    if "nav" not in st.session_state:
        st.session_state["nav"] = "Proyectos"

    with st.sidebar:
        _sidebar(settings, store)

    page = st.session_state["nav"]
    if page == "Proyectos":
        page_projects.render(store)
        return

    # El proveedor y la cola solo son necesarios fuera de la página de proyectos.
    provider = get_provider(settings)
    runner = get_runner(settings)

    if page == "Proyecto":
        page_project.render(store, settings)
    elif page == "Lote":
        page_batch.render(store, settings, provider, runner)
    elif page == "Resultados":
        page_results.render(store, settings, provider, runner)
    else:  # pragma: no cover - red de seguridad
        st.session_state["nav"] = "Proyectos"
        st.rerun()
