"""Adaptador REST de Higgsfield (https://api.higgsfield.ai).

Verificado en la documentación oficial (2026-10-19):
- Autenticación: cabecera ``Authorization: Key <credencial completa>`` (verbatim,
  el formato de la clave no se altera). Solo viaja al backend local/nunca a logs.
- Submit:  ``POST /{model-path}`` con cabecera ``Idempotency-Key`` -> request_id.
- Estado:  ``GET  /requests/{request_id}/status`` (terminal: completed/failed/
  nsfw/canceled).
- Cancel:  ``POST /requests/{request_id}/cancel`` (solo trabajos aún en cola).
- Upload:  ``POST /files/generate-upload-url`` + ``PUT`` a la URL firmada con los
  ``upload_headers`` devueltos y SIN credenciales de la API.
- Costes:  ``POST /estimate/{model-path}`` -> {credits, usd}.

Ninguna función de este módulo imprime la clave ni las URLs firmadas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..models import MODELS, ModelSpec
from .base import Estimate, ProviderError, RemoteStatus, SubmitResult

DEFAULT_BASE_URL = "https://api.higgsfield.ai"
DEFAULT_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0

_CONTENT_TYPE_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _extract_detail(response: httpx.Response) -> str:
    """Extrae el mensaje del envelope de error FastAPI sin inventar texto."""
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] if text else f"HTTP {response.status_code}"
    if isinstance(data, dict):
        detail = data.get("detail", data)
        if isinstance(detail, list):
            return "; ".join(str(item) for item in detail)[:500]
        return str(detail)[:500]
    return str(data)[:500]


def _classify_error(response: httpx.Response) -> ProviderError:
    """Mapea los códigos documentados a errores con reintentabilidad correcta."""
    status = response.status_code
    detail = _extract_detail(response)
    code = "http_error"
    retryable = False

    if status == 400:
        code = "bad_request"
        # 400 cubre también "concurrency reached": esperar y reintentar.
        retryable = "concurrency" in detail.lower()
    elif status == 401:
        code = "unauthorized"
    elif status == 403:
        code = "insufficient_credits"
        detail = f"Créditos insuficientes o acceso denegado: {detail}"
    elif status == 404:
        code = "not_found"
    elif status == 422:
        code = "unprocessable"
        if "idempotency" in detail.lower():
            detail += " (clave de idempotencia reutilizada con parámetros distintos)"
    elif status == 423:
        code = "model_blocked"
        retryable = True
    elif status == 429:
        code = "rate_limited"
        retryable = True
    elif status >= 500:
        code = "server_error"
        retryable = True

    return ProviderError(
        detail or f"HTTP {status}",
        code=code,
        http_status=status,
        retryable=retryable,
    )


class HiggsfieldProvider:
    """Implementación REST del contrato ImageProvider."""

    name = "higgsfield"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ProviderError(
                "Falta la clave de Higgsfield (HF_API_KEY). "
                "Obténla en https://open.higgsfield.ai/api-keys y guárdala en .env.",
                code="missing_api_key",
            )
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    # -- helpers ---------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._api_key}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._client.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Tiempo de espera agotado contra la API de Higgsfield.",
                code="timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"Error de red contra la API de Higgsfield: {type(exc).__name__}.",
                code="network",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise _classify_error(response)
        return response

    # -- contrato ImageProvider -----------------------------------------

    def model_specs(self) -> list[ModelSpec]:
        return list(MODELS.values())

    def upload_reference(self, path: Path, content_type: str) -> str:
        """URL firmada -> PUT sin credenciales -> public_url."""
        data = self._request(
            "POST",
            f"{self._base_url}/files/generate-upload-url",
            json_body={"content_type": content_type},
        ).json()
        upload_url = data.get("upload_url")
        public_url = data.get("public_url")
        upload_headers = data.get("upload_headers") or {}
        if not upload_url or not public_url:
            raise ProviderError(
                "Respuesta inválida de generate-upload-url (sin upload_url/public_url).",
                code="invalid_upload_response",
            )

        file_bytes = path.read_bytes()
        try:
            # Importante: sin cabecera Authorization hacia el storage firmado.
            put_response = self._client.put(
                upload_url,
                content=file_bytes,
                headers={str(k): str(v) for k, v in upload_headers.items()},
                timeout=DOWNLOAD_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Tiempo de espera agotado subiendo la referencia.",
                code="timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"Error de red subiendo la referencia: {type(exc).__name__}.",
                code="network",
                retryable=True,
            ) from exc
        if put_response.status_code >= 400:
            raise ProviderError(
                f"La subida de la referencia falló (HTTP {put_response.status_code}).",
                code="upload_failed",
                http_status=put_response.status_code,
                retryable=put_response.status_code >= 500,
            )
        return str(public_url)

    def estimate(self, model_id: str, payload: dict[str, Any]) -> Estimate | None:
        try:
            data = self._request(
                "POST",
                f"{self._base_url}/estimate/{model_id}",
                json_body=payload,
            ).json()
        except ProviderError as exc:
            # Modelo sin endpoint de estimación documentado -> coste no disponible.
            if exc.code == "not_found":
                return None
            raise
        if not isinstance(data, dict):
            return None
        estimate = Estimate(
            credits=str(data["credits"]) if data.get("credits") is not None else None,
            usd=str(data["usd"]) if data.get("usd") is not None else None,
            raw=data,
        )
        return estimate if estimate.has_value else None

    def submit(
        self, model_id: str, payload: dict[str, Any], idempotency_key: str
    ) -> SubmitResult:
        response = self._request(
            "POST",
            f"{self._base_url}/{model_id}",
            json_body=payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        data = response.json()
        request_id = data.get("request_id")
        if not request_id:
            raise ProviderError(
                "La respuesta del envío no incluye request_id.",
                code="invalid_submit_response",
            )
        return SubmitResult(
            request_id=str(request_id),
            status_url=data.get("status_url"),
            cancel_url=data.get("cancel_url"),
        )

    def status(self, request_id: str) -> RemoteStatus:
        data = self._request(
            "GET",
            f"{self._base_url}/requests/{request_id}/status",
        ).json()
        if not isinstance(data, dict) or not data.get("status"):
            raise ProviderError(
                "Respuesta de estado inválida (sin 'status').",
                code="invalid_status_response",
            )
        images = data.get("images") or []
        urls = tuple(
            str(img["url"])
            for img in images
            if isinstance(img, dict) and img.get("url")
        )
        error = data.get("error")
        return RemoteStatus(
            status=str(data["status"]),
            image_urls=urls,
            error=str(error) if error else None,
        )

    def cancel(self, request_id: str) -> bool:
        self._request(
            "POST",
            f"{self._base_url}/requests/{request_id}/cancel",
            json_body={},
        )
        return True

    def download(self, url: str, dest: Path) -> Path:
        try:
            with self._client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=DOWNLOAD_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _classify_error(response)
                final_dest = dest
                if final_dest.suffix.lower() not in _CONTENT_TYPE_SUFFIX.values():
                    # "original-v1" no es una extensión: anexar la del contenido.
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    suffix = _CONTENT_TYPE_SUFFIX.get(content_type.strip().lower())
                    if not suffix:
                        url_suffix = Path(url.split("?")[0]).suffix.lower()
                        suffix = (
                            url_suffix
                            if url_suffix in _CONTENT_TYPE_SUFFIX.values()
                            else ".png"
                        )
                    final_dest = Path(f"{dest}{suffix}")
                final_dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(final_dest, "wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                except OSError as exc:
                    final_dest.unlink(missing_ok=True)
                    raise ProviderError(
                        f"Error escribiendo el resultado en disco: {exc}",
                        code="disk_error",
                        retryable=True,
                    ) from exc
                return final_dest
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Tiempo de espera agotado descargando el resultado.",
                code="timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"Error de red descargando el resultado: {type(exc).__name__}.",
                code="network",
                retryable=True,
            ) from exc

    def close(self) -> None:
        self._client.close()
