"""Configuración de Genesis VisualForge a partir de variables de entorno.

Lee `.env` (ignorado por git) con la misma API key que proporciona el usuario.
Nunca se imprimen ni registran valores de secretos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import MAX_REFERENCE_MB_DEFAULT, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]

PROVIDER_MOCK = "mock"
PROVIDER_HIGGSFIELD = "higgsfield"


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    provider: str  # "mock" | "higgsfield"
    concurrency: int
    projects_dir: Path
    max_reference_mb: int

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def _resolve_projects_dir(raw: str | None) -> Path:
    if not raw:
        return REPO_ROOT / "projects"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def load_settings(env_file: Path | None = None) -> Settings:
    """Carga la configuración. `.env` del repositorio si existe (no lo pisa)."""
    load_dotenv(env_file or (REPO_ROOT / ".env"), override=False)

    api_key = os.environ.get("HF_API_KEY", "").strip() or None

    raw_provider = os.environ.get("VISUALFORGE_PROVIDER", "").strip().lower()
    if raw_provider in {PROVIDER_MOCK, PROVIDER_HIGGSFIELD}:
        provider = raw_provider
    elif raw_provider:
        raise ValidationError(
            f"VISUALFORGE_PROVIDER inválido: '{raw_provider}'. Use 'mock' o 'higgsfield'."
        )
    else:
        # Sin clave no tiene sentido el proveedor real: mock por defecto.
        provider = PROVIDER_HIGGSFIELD if api_key else PROVIDER_MOCK

    concurrency_raw = os.environ.get("VISUALFORGE_CONCURRENCY", "").strip()
    try:
        concurrency = int(concurrency_raw) if concurrency_raw else 2
    except ValueError as exc:
        raise ValidationError(
            f"VISUALFORGE_CONCURRENCY inválido: '{concurrency_raw}' (entero requerido)."
        ) from exc
    concurrency = max(1, min(concurrency, 8))

    max_mb_raw = os.environ.get("VISUALFORGE_MAX_REF_MB", "").strip()
    try:
        max_reference_mb = int(max_mb_raw) if max_mb_raw else MAX_REFERENCE_MB_DEFAULT
    except ValueError as exc:
        raise ValidationError(
            f"VISUALFORGE_MAX_REF_MB inválido: '{max_mb_raw}' (entero requerido)."
        ) from exc

    return Settings(
        api_key=api_key,
        provider=provider,
        concurrency=concurrency,
        projects_dir=_resolve_projects_dir(os.environ.get("VISUALFORGE_PROJECTS_DIR")),
        max_reference_mb=max_reference_mb,
    )
