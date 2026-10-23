"""Proveedor simulado: mismos contratos que el real, sin red ni coste.

Se usa en tests y en modo `VISUALFORGE_PROVIDER=mock`. Reproduce los
comportamientos críticos del proveedor real para poder probarlos:

- Idempotencia: la misma ``idempotency_key`` devuelve el mismo ``request_id``
  y solo cuenta como UNA generación (evita cargos duplicados).
- Fallo ambiguo: primer submit aceptado pero respuesta perdida -> reintento
  con la misma clave devuelve el ``request_id`` original.
- Estados terminales y cancelación solo antes de completar.
- Descarga de resultados como PNG determinista generado con Pillow.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from ..models import MODELS, ModelSpec
from .base import Estimate, ProviderError, RemoteStatus, SubmitResult

MOCK_REFERENCE_SCHEME = "mock://reference/"
MOCK_IMAGE_SCHEME = "mock://image/"


@dataclass
class MockRequest:
    request_id: str
    model_id: str
    payload: dict[str, Any]
    polls: int = 0
    done: bool = False
    canceled: bool = False
    in_flight: bool = True


@dataclass
class MockScenarios:
    """Inyectabilidad de fallos para tests."""

    submit_failures: int = 0  # N fallos de red ANTES de aceptar (clave no consumida)
    accept_then_lose_response: bool = False  # acepta la 1ª y "pierde" la respuesta
    reject_submits: bool = False  # error no reintentable (p. ej. 401)
    status_failures: int = 0  # N fallos de red consultando estado
    fail_terminal: bool = False  # termina en "failed" en vez de "completed"
    nsfw_terminal: bool = False  # termina en "nsfw" (moderación)
    download_failures: int = 0  # N fallos descargando el resultado
    polls_to_complete: int = 2  # consultas de estado hasta completar
    estimate_available: bool = True


@dataclass
class MockProvider:
    """Implementación ImageProvider sin consumo de créditos."""

    name: str = "mock"
    scenarios: MockScenarios = field(default_factory=MockScenarios)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[str, str] = {}  # idempotency_key -> request_id
        self._requests: dict[str, MockRequest] = {}
        # Métricas para tests
        self.charged_keys: list[str] = []
        self.submit_calls: int = 0
        self.max_in_flight: int = 0
        self._in_flight: int = 0

    # -- contrato ImageProvider -----------------------------------------

    def model_specs(self) -> list[ModelSpec]:
        return list(MODELS.values())

    def upload_reference(self, path: Path, content_type: str) -> str:
        if not path.is_file():
            raise ProviderError(
                f"Referencia no encontrada: {path.name}",
                code="not_found",
            )
        return f"{MOCK_REFERENCE_SCHEME}{path.name}"

    def estimate(self, model_id: str, payload: dict[str, Any]) -> Estimate | None:
        if not self.scenarios.estimate_available:
            return None
        return Estimate(credits="1.000", usd="0.050", raw={"mock": True})

    def submit(
        self, model_id: str, payload: dict[str, Any], idempotency_key: str
    ) -> SubmitResult:
        with self._lock:
            self.submit_calls += 1

            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                # Reintento idempotente: misma generación, sin nueva carga.
                return SubmitResult(
                    request_id=existing,
                    status_url=f"mock://status/{existing}",
                    cancel_url=f"mock://cancel/{existing}",
                )

            if self.scenarios.reject_submits:
                raise ProviderError(
                    "Credenciales inválidas (mock 401).",
                    code="unauthorized",
                    http_status=401,
                    retryable=False,
                )

            if self.scenarios.submit_failures > 0:
                self.scenarios.submit_failures -= 1
                raise ProviderError(
                    "Fallo de red simulado en el envío (mock).",
                    code="network",
                    retryable=True,
                )

            request_id = f"mock-{uuid.uuid4().hex[:12]}"

            if self.scenarios.accept_then_lose_response:
                # Aceptado por el "servidor" pero el cliente no recibe la respuesta.
                self._by_key[idempotency_key] = request_id
                self._requests[request_id] = MockRequest(
                    request_id=request_id, model_id=model_id, payload=dict(payload)
                )
                self.charged_keys.append(idempotency_key)
                self._register_in_flight_locked()
                self.scenarios.accept_then_lose_response = False
                raise ProviderError(
                    "Tiempo de espera agotado tras enviar (respuesta perdida, mock).",
                    code="timeout",
                    retryable=True,
                )

            self._by_key[idempotency_key] = request_id
            self._requests[request_id] = MockRequest(
                request_id=request_id, model_id=model_id, payload=dict(payload)
            )
            self.charged_keys.append(idempotency_key)
            self._register_in_flight_locked()
            return SubmitResult(
                request_id=request_id,
                status_url=f"mock://status/{request_id}",
                cancel_url=f"mock://cancel/{request_id}",
            )

    def status(self, request_id: str) -> RemoteStatus:
        with self._lock:
            if self.scenarios.status_failures > 0:
                self.scenarios.status_failures -= 1
                raise ProviderError(
                    "Fallo de red simulado consultando estado (mock).",
                    code="network",
                    retryable=True,
                )

            req = self._requests.get(request_id)
            if req is None:
                raise ProviderError(
                    f"Request no encontrado: {request_id}",
                    code="not_found",
                    http_status=404,
                    retryable=False,
                )
            if req.canceled:
                return RemoteStatus(status="canceled")
            if req.done:
                return self._terminal_remote_status(req)

            req.polls += 1
            if req.polls >= self.scenarios.polls_to_complete:
                req.done = True
                self._release_in_flight_locked()
                return self._terminal_remote_status(req)
            current = "queued" if req.polls == 1 else "running"
            return RemoteStatus(status=current)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ProviderError(
                    f"Request no encontrado: {request_id}",
                    code="not_found",
                    http_status=404,
                    retryable=False,
                )
            if req.done:
                raise ProviderError(
                    "La solicitud ya finalizó y no puede cancelarse (mock).",
                    code="not_cancelable",
                )
            if not req.canceled:
                req.canceled = True
                self._release_in_flight_locked()
            return True

    def download(self, url: str, dest: Path) -> Path:
        if not url.startswith(MOCK_IMAGE_SCHEME):
            raise ProviderError(
                f"URL no soportada por el proveedor mock: {url}",
                code="bad_request",
            )
        with self._lock:
            if self.scenarios.download_failures > 0:
                self.scenarios.download_failures -= 1
                raise ProviderError(
                    "Fallo de red simulado descargando (mock).",
                    code="network",
                    retryable=True,
                )

        final_dest = dest
        if final_dest.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            final_dest = Path(f"{dest}.png")
        final_dest.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.md5(url.encode("utf-8")).digest()
        color = (digest[0], digest[1], digest[2])
        image = Image.new("RGB", (768, 512), color=color)
        image.save(final_dest, format="PNG")
        return final_dest

    # -- helpers ---------------------------------------------------------

    def _terminal_remote_status(self, req: MockRequest) -> RemoteStatus:
        if self.scenarios.nsfw_terminal:
            return RemoteStatus(status="nsfw")
        if self.scenarios.fail_terminal:
            return RemoteStatus(status="failed", error="Generación fallida (mock)")
        url = f"{MOCK_IMAGE_SCHEME}{req.request_id}"
        return RemoteStatus(status="completed", image_urls=(url,))

    def _register_in_flight_locked(self) -> None:
        self._in_flight += 1
        if self._in_flight > self.max_in_flight:
            self.max_in_flight = self._in_flight

    def _release_in_flight_locked(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1
