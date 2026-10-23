"""Interfaz desacoplada del proveedor de generación de imágenes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..models import ModelSpec


class ProviderError(Exception):
    """Error del proveedor con clasificación de reintentabilidad."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True)
class Estimate:
    """Estimación de coste devuelta por la API (cuando existe y es fiable)."""

    credits: str | None = None
    usd: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_value(self) -> bool:
        return bool(self.usd or self.credits)


@dataclass(frozen=True)
class SubmitResult:
    request_id: str
    status_url: str | None = None
    cancel_url: str | None = None


@dataclass(frozen=True)
class RemoteStatus:
    """Estado normalizado de un request remoto.

    ``status`` usa valores crudos de la API (queued/running/completed/failed/
    nsfw/canceled); la conversión a estados locales ocurre en jobs.py.
    """

    status: str
    image_urls: tuple[str, ...] = ()
    error: str | None = None


@runtime_checkable
class ImageProvider(Protocol):
    """Contrato mínimo que debe cumplir cualquier proveedor (MVP: uno solo)."""

    name: str

    def model_specs(self) -> list[ModelSpec]:
        """Modelos documentados y sus opciones válidas."""
        ...

    def upload_reference(self, path: Path, content_type: str) -> str:
        """Sube una referencia y devuelve su URL pública usable en image_urls."""
        ...

    def estimate(self, model_id: str, payload: dict[str, Any]) -> Estimate | None:
        """Estimación de coste; None si el proveedor no la soporta."""
        ...

    def submit(
        self, model_id: str, payload: dict[str, Any], idempotency_key: str
    ) -> SubmitResult:
        """Envía una generación. La clave debe reutilizarse ante fallos ambiguos."""
        ...

    def status(self, request_id: str) -> RemoteStatus:
        """Estado actual del request."""
        ...

    def cancel(self, request_id: str) -> bool:
        """Cancela en el proveedor. Detener el polling NO cancela."""
        ...

    def download(self, url: str, dest: Path) -> Path:
        """Descarga un resultado a disco y devuelve la ruta escrita."""
        ...
