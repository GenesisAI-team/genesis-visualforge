"""Configuración de entorno: provider, credenciales, concurrencia y rutas."""

from __future__ import annotations

from pathlib import Path

import pytest

from visualforge.config import REPO_ROOT, load_settings, ui_default_concurrency
from visualforge.models import UI_CONCURRENCY_MAX, UI_CONCURRENCY_MIN, ValidationError


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


def test_clave_presente_sin_provider_mantiene_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GEN-30: la sola presencia de HF_API_KEY NO activa el proveedor real."""
    monkeypatch.setenv("HF_API_KEY", "key-id:key-secret")
    settings = load_settings()
    assert settings.provider == "mock"
    assert settings.has_api_key is True


def test_provider_higgsfield_explicito_con_clave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La selección del proveedor real es siempre deliberada (GEN-30)."""
    monkeypatch.setenv("HF_API_KEY", "key-id:key-secret")
    monkeypatch.setenv("VISUALFORGE_PROVIDER", "higgsfield")
    settings = load_settings()
    assert settings.provider == "higgsfield"
    assert settings.api_key == "key-id:key-secret"


def test_concurrencia_se_limita(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUALFORGE_CONCURRENCY", "99")
    assert load_settings().concurrency == 8
    monkeypatch.setenv("VISUALFORGE_CONCURRENCY", "0")
    assert load_settings().concurrency == 1


def test_gen15_ui_default_concurrency_recorta_al_rango_del_slider() -> None:
    """GEN-15: el default del slider queda SIEMPRE dentro de 1..4.

    El valor de arranque de ``.env`` (1-8) es una preferencia, no un techo:
    por encima de 4 solo recorta el valor inicial del control, la UI nunca
    se amplía a 8 y con ``.env=8`` el slider se sigue creando sin romperse.
    """
    assert ui_default_concurrency(0) == 1
    assert ui_default_concurrency(1) == 1
    assert ui_default_concurrency(2) == 2   # default: 2
    assert ui_default_concurrency(3) == 3
    assert ui_default_concurrency(4) == 4
    assert ui_default_concurrency(5) == 4
    assert ui_default_concurrency(8) == 4   # .env=8 no rompe la creación de controles

    for value in range(-5, 20):
        clipped = ui_default_concurrency(value)
        assert UI_CONCURRENCY_MIN <= clipped <= UI_CONCURRENCY_MAX


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
