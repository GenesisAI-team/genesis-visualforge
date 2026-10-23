"""Configuración de entorno: provider, credenciales, concurrencia y rutas."""

from __future__ import annotations

from pathlib import Path

import pytest

from visualforge.config import REPO_ROOT, load_settings
from visualforge.models import ValidationError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla de .env real del repositorio y de variables del entorno."""
    monkeypatch.setattr("visualforge.config.load_dotenv", lambda *a, **k: None)
    for key in (
        "HF_API_KEY",
        "VISUALFORGE_PROVIDER",
        "VISUALFORGE_CONCURRENCY",
        "VISUALFORGE_PROJECTS_DIR",
        "VISUALFORGE_MAX_REF_MB",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults_sin_env() -> None:
    settings = load_settings()
    assert settings.provider == "mock"
    assert settings.has_api_key is False
    assert settings.concurrency == 2
    assert settings.projects_dir == REPO_ROOT / "projects"


def test_provider_higgsfield_sin_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_PROVIDER", "higgsfield")
    settings = load_settings()
    assert settings.provider == "higgsfield"
    assert settings.has_api_key is False


def test_clave_presente_activa_higgsfield(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_API_KEY", "key-id:key-secret")
    settings = load_settings()
    assert settings.provider == "higgsfield"
    assert settings.has_api_key is True


def test_concurrencia_se_limita(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_CONCURRENCY", "99")
    assert load_settings().concurrency == 8
    monkeypatch.setenv("VISUALFORGE_CONCURRENCY", "0")
    assert load_settings().concurrency == 1


def test_provider_invalido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_PROVIDER", "openai")
    with pytest.raises(ValidationError, match="inválido"):
        load_settings()


def test_concurrencia_invalida(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_CONCURRENCY", "muchos")
    with pytest.raises(ValidationError, match="CONCURRENCY"):
        load_settings()


def test_projects_dir_override_relativo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_PROJECTS_DIR", "otro/dir")
    assert load_settings().projects_dir == REPO_ROOT / "otro/dir"


def test_projects_dir_override_absoluto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VISUALFORGE_PROJECTS_DIR", str(tmp_path))
    assert load_settings().projects_dir == tmp_path


def test_max_ref_mb_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_MAX_REF_MB", "5")
    assert load_settings().max_reference_mb == 5
