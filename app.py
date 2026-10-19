"""Genesis VisualForge — punto de entrada de la interfaz Streamlit.

Ejecutar desde la raíz del repositorio:

    streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Permite ejecutar sin instalar el paquete (útil para agentes y pruebas rápidas).
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

st.set_page_config(
    page_title="Genesis VisualForge",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="expanded",
)

from visualforge.ui import router  # noqa: E402

router()
