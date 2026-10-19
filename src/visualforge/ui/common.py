"""Comunes de UI: acceso a store/proveedor/runner, badges y formato."""

from __future__ import annotations

import hashlib

import streamlit as st

from ..config import PROVIDER_HIGGSFIELD, PROVIDER_MOCK, Settings, load_settings
from ..jobs import BatchRunner, RunnerConfig
from ..models import Job, JobStatus, Project, ValidationError
from ..provider import ImageProvider, MockProvider, ProviderError
from ..provider.higgsfield import HiggsfieldProvider
from ..storage import Store

STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.PENDING: "Pendiente",
    JobStatus.QUEUED: "En cola",
    JobStatus.RUNNING: "Generando",
    JobStatus.COMPLETED: "Completado",
    JobStatus.FAILED: "Fallido",
    JobStatus.CANCELED: "Cancelado",
}

STATUS_ICONS: dict[JobStatus, str] = {
    JobStatus.PENDING: "🕓",
    JobStatus.QUEUED: "⏳",
    JobStatus.RUNNING: "⚙️",
    JobStatus.COMPLETED: "✅",
    JobStatus.FAILED: "❌",
    JobStatus.CANCELED: "🚫",
}


def get_settings() -> Settings:
    try:
        return load_settings()
    except ValidationError as exc:
        st.error(f"Configuración inválida: {exc}")
        st.stop()
    raise RuntimeError("inalcanzable")  # pragma: no cover


@st.cache_resource(show_spinner=False)
def _store_cached(projects_dir: str, max_reference_mb: int) -> Store:
    from pathlib import Path

    return Store(Path(projects_dir), max_reference_mb=max_reference_mb)


@st.cache_resource(show_spinner=False)
def _provider_cached(name: str, api_key: str) -> ImageProvider:
    if name == PROVIDER_MOCK:
        return MockProvider()
    return HiggsfieldProvider(api_key)


def get_store(settings: Settings | None = None) -> Store:
    settings = settings or get_settings()
    return _store_cached(str(settings.projects_dir), settings.max_reference_mb)


def _session_api_key() -> str | None:
    value = st.session_state.get("hf_api_key_session")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_provider(settings: Settings | None = None) -> ImageProvider:
    """Proveedor activo: clave de sesión si se introdujo, si no la de .env."""
    settings = settings or get_settings()
    if settings.provider == PROVIDER_MOCK:
        return _provider_cached(PROVIDER_MOCK, "")
    api_key = _session_api_key() or settings.api_key
    if not api_key:
        st.warning(
            "Falta la clave de Higgsfield. Añádela en `.env` (`HF_API_KEY=...`) "
            "o en el panel lateral «Clave de API»."
        )
        st.stop()
    cache_id = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return _provider_cached(PROVIDER_HIGGSFIELD, f"{cache_id}:{api_key}")


@st.cache_resource(show_spinner=False)
def _runner_cached(
    projects_dir: str,
    max_reference_mb: int,
    provider_name: str,
    provider_key: str,
    concurrency: int,
) -> BatchRunner:
    from pathlib import Path

    store = Store(Path(projects_dir), max_reference_mb=max_reference_mb)
    if provider_name == PROVIDER_MOCK:
        provider: ImageProvider = MockProvider()
    else:
        provider = HiggsfieldProvider(provider_key)
    fast = provider_name == PROVIDER_MOCK
    runner = BatchRunner(
        store,
        provider,
        RunnerConfig(
            concurrency=concurrency,
            poll_initial=0.4 if fast else 2.0,
            poll_max=1.0 if fast else 10.0,
            poll_jitter=0.1 if fast else 0.5,
        ),
    )
    resumed = runner.resume()
    if resumed:
        st.session_state["vf_resumed"] = resumed
    return runner


def get_runner(settings: Settings | None = None) -> BatchRunner:
    settings = settings or get_settings()
    if settings.provider == PROVIDER_MOCK:
        provider_name, provider_key = PROVIDER_MOCK, ""
    else:
        provider_name = PROVIDER_HIGGSFIELD
        provider_key = _session_api_key() or settings.api_key or ""
        if not provider_key:
            st.warning(
                "Configura `HF_API_KEY` en `.env` (ver `.env.example`) para usar "
                "Higgsfield, o cambia `VISUALFORGE_PROVIDER=mock` para probar sin gasto."
            )
            st.stop()
    return _runner_cached(
        str(settings.projects_dir),
        settings.max_reference_mb,
        provider_name,
        provider_key,
        settings.concurrency,
    )


def provider_badge(settings: Settings) -> None:
    if settings.provider == PROVIDER_MOCK:
        st.info(
            "**Proveedor: SIMULACIÓN (mock)** — no se consume la API ni créditos. "
            "Ideal para probar la interfaz y los tests."
        )
    else:
        st.warning(
            "**Proveedor: Higgsfield API (producción)** — cada lote consumirá "
            "créditos reales de tu cuenta. Se pedirá confirmación antes de enviar."
        )


def api_key_panel(settings: Settings) -> None:
    """Panel lateral opcional para introducir la clave en sesión local."""
    if settings.provider == PROVIDER_MOCK:
        return
    if settings.has_api_key:
        st.caption("🔑 `HF_API_KEY` cargada desde `.env` (no se muestra ni se guarda en la app).")
        return
    with st.expander("🔑 Clave de API de Higgsfield"):
        st.caption(
            "Se usa solo en esta sesión local; nunca se envía al navegador ni a logs. "
            "Consíguela en https://open.higgsfield.ai/api-keys"
        )
        st.text_input(
            "HF_API_KEY (pega la clave completa)",
            type="password",
            key="hf_api_key_session",
            label_visibility="collapsed",
        )


def job_status_mark(job: Job) -> str:
    return f"{STATUS_ICONS[job.status]} {STATUS_LABELS[job.status]}"


def fmt_usd(value: str | None) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):.3f}"
    except ValueError:
        return str(value)


def fmt_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, size // 1024)} KB"


def current_project(store: Store) -> Project | None:
    slug = st.session_state.get("current_project_slug")
    if not slug:
        return None
    try:
        return store.load_project(slug)
    except ValidationError:
        st.session_state.pop("current_project_slug", None)
        return None


def go(page: str, project_slug: str | None = None) -> None:
    """Navegación programática (el radio lateral usa la clave 'nav')."""
    if project_slug is not None:
        st.session_state["current_project_slug"] = project_slug
    st.session_state["nav"] = page
    st.rerun()


def estimate_total(jobs: list[Job]) -> tuple[str | None, bool]:
    """Suma de estimaciones; (texto, True) si todo tiene coste fiable."""
    values: list[float] = []
    complete = bool(jobs)
    for job in jobs:
        if job.estimate_usd is None:
            complete = False
            continue
        try:
            values.append(float(job.estimate_usd))
        except ValueError:
            complete = False
    if not values:
        return None, complete
    return f"${sum(values):.3f}", complete


def ensure_provider_ready(provider: ImageProvider) -> bool:
    try:
        provider.model_specs()
    except ProviderError as exc:
        st.error(f"Proveedor no disponible: {exc}")
        return False
    return True


def open_folder(path) -> None:
    """Abre una carpeta en el explorador del sistema (mejor esfuerzo)."""
    import os
    import subprocess
    from pathlib import Path

    folder = Path(path)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(folder))  # noqa: S606
        elif os.name == "posix":
            subprocess.run(["xdg-open", str(folder)], check=False)
    except OSError:
        st.info(f"Carpeta: `{folder}`")
