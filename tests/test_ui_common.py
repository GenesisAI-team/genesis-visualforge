"""GEN-30: regresión de credencial y selección deliberada de proveedor.

Garantiza que la credencial suministrada por el usuario (``.env`` o panel de
sesión) llega INTACTA a ``HiggsfieldProvider`` y a su cabecera ``Authorization``.
Todo con valores sintéticos: sin claves reales ni red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from visualforge.config import PROVIDER_HIGGSFIELD, PROVIDER_MOCK, Settings
from visualforge.provider import MockProvider
from visualforge.provider.base import ImageProvider, ProviderError
from visualforge.provider.higgsfield import HiggsfieldProvider
from visualforge.ui import common

_CRED_ENV = "credencial-sintetica-desde-env"
_CRED_SESION = "credencial-sintetica-desde-sesion"


def make_settings(provider: str, api_key: str | None) -> Settings:
    return Settings(
        api_key=api_key,
        provider=provider,
        concurrency=2,
        projects_dir=Path("projects"),
        max_reference_mb=25,
    )


class ProviderRecorder:
    """Sustituye a la caché de recursos y registra las credenciales recibidas."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, name: str, api_key: str) -> ImageProvider:
        self.calls.append((name, api_key))
        return MockProvider()


def _mock_provider_cached(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    recorder = ProviderRecorder()
    monkeypatch.setattr(common, "_provider_cached", recorder)
    return recorder.calls


# ---------------------------------------------------------------------------
# Fábrica pura: credencial intacta y recambio de clave
# ---------------------------------------------------------------------------


def test_fabrica_higgsfield_construye_con_la_credencial_intacta() -> None:
    provider = common._build_provider(PROVIDER_HIGGSFIELD, _CRED_ENV)
    try:
        assert isinstance(provider, HiggsfieldProvider)
        # Cabecera EXACTA con la credencial tal cual: sin prefijos ni reformateos.
        assert provider._auth_headers() == {"Authorization": f"Key {_CRED_ENV}"}
    finally:
        provider.close()


def test_fabrica_mock_ignora_credencial() -> None:
    assert isinstance(common._build_provider(PROVIDER_MOCK, _CRED_ENV), MockProvider)


def test_fabrica_cambia_de_instancia_al_cambiar_de_credencial() -> None:
    """Recambio de clave: nueva instancia, ninguna retiene la credencial ajena."""
    primero = common._build_provider(PROVIDER_HIGGSFIELD, "cred-A")
    segundo = common._build_provider(PROVIDER_HIGGSFIELD, "cred-B")
    try:
        assert primero is not segundo
        assert primero._auth_headers() == {"Authorization": "Key cred-A"}
        assert segundo._auth_headers() == {"Authorization": "Key cred-B"}
    finally:
        primero.close()
        segundo.close()


# ---------------------------------------------------------------------------
# get_provider: la credencial se pasa sin manipular a la caché/adaptador
# ---------------------------------------------------------------------------


def test_get_provider_envia_credencial_de_env_intacta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_provider_cached(monkeypatch)
    monkeypatch.setattr(common, "_session_api_key", lambda: None)

    common.get_provider(make_settings(PROVIDER_HIGGSFIELD, _CRED_ENV))

    assert calls == [(PROVIDER_HIGGSFIELD, _CRED_ENV)]


def test_get_provider_prefiere_la_clave_de_sesion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_provider_cached(monkeypatch)
    monkeypatch.setattr(common, "_session_api_key", lambda: _CRED_SESION)

    common.get_provider(make_settings(PROVIDER_HIGGSFIELD, _CRED_ENV))

    assert calls == [(PROVIDER_HIGGSFIELD, _CRED_SESION)]


def test_get_provider_mock_no_necesita_credencial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_provider_cached(monkeypatch)
    monkeypatch.setattr(common, "_session_api_key", lambda: _CRED_SESION)

    common.get_provider(make_settings(PROVIDER_MOCK, None))

    assert calls == [(PROVIDER_MOCK, "")]


def test_provider_error_message_distingue_codigos() -> None:
    cases = {
        "unauthorized": "(401)",
        "insufficient_credits": "(403)",
        "rate_limited": "(429)",
        "missing_api_key": "no hay credencial cargada",
        "network": "sin cambiar parámetros",
    }
    for code, expected_substring in cases.items():
        message = common._provider_error_message(
            ProviderError("detalle técnico", code=code)
        )
        assert expected_substring in message
        assert "detalle técnico" in message
