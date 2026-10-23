"""Fábrica de proveedores."""

from __future__ import annotations

from ..config import PROVIDER_HIGGSFIELD, PROVIDER_MOCK, Settings
from .base import (
    Estimate,
    ImageProvider,
    ProviderError,
    RemoteStatus,
    SubmitResult,
)
from .higgsfield import HiggsfieldProvider
from .mock import MockProvider, MockScenarios

__all__ = [
    "Estimate",
    "HiggsfieldProvider",
    "ImageProvider",
    "MockProvider",
    "MockScenarios",
    "ProviderError",
    "RemoteStatus",
    "SubmitResult",
    "build_provider",
]


def build_provider(settings: Settings) -> ImageProvider:
    """Construye el proveedor activo a partir de la configuración."""
    if settings.provider == PROVIDER_MOCK:
        return MockProvider()
    if settings.provider == PROVIDER_HIGGSFIELD:
        if not settings.api_key:
            raise ProviderError(
                "Proveedor 'higgsfield' seleccionado sin HF_API_KEY. "
                "Configúrala en .env (ver .env.example) o usa 'mock'.",
                code="missing_api_key",
            )
        return HiggsfieldProvider(settings.api_key)
    raise ProviderError(
        f"Proveedor desconocido: '{settings.provider}'.",
        code="unknown_provider",
    )
