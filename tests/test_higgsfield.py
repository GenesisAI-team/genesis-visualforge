"""Adapter Higgsfield con httpx.MockTransport: sin red, sin cargos.

Verifica el contrato real documentado: cabeceras de auth/idempotencia,
PUT de subida SIN credenciales, mapeo de errores y clasificación de reintentos.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from visualforge.provider.base import ProviderError
from visualforge.provider.higgsfield import HiggsfieldProvider

MODEL = "marketing-studio/image/flare"


class Recorder:
    """Transporte mockeado que registra peticiones y responde por ruta."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.handler = None  # callable opcional(request) -> Response

    def add(self, method: str, url: str, response: httpx.Response) -> None:
        self.routes[(method.upper(), url)] = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        key = (request.method.upper(), str(request.url))
        if key in self.routes:
            return self.routes[key]
        return httpx.Response(404, json={"detail": f"sin ruta {key}"})

    def by_method_path(self, method: str, path: str) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method.upper() == method.upper() and r.url.path == path
        ]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def make_provider(recorder: Recorder) -> HiggsfieldProvider:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return HiggsfieldProvider("key-id:key-secret", client=client)


def test_submit_envia_auth_e_idempotency_key(recorder: Recorder) -> None:
    recorder.add(
        "POST",
        f"https://api.higgsfield.ai/{MODEL}",
        httpx.Response(200, json={"request_id": "req-1"}),
    )
    provider = make_provider(recorder)
    result = provider.submit(MODEL, {"prompt": "hola"}, "idem-123")
    assert result.request_id == "req-1"
    request = recorder.by_method_path("POST", f"/{MODEL}")[0]
    assert request.headers["Authorization"] == "Key key-id:key-secret"
    assert request.headers["Idempotency-Key"] == "idem-123"
    assert json.loads(request.content) == {"prompt": "hola"}


def test_submit_sin_request_id(recorder: Recorder) -> None:
    recorder.add(
        "POST",
        f"https://api.higgsfield.ai/{MODEL}",
        httpx.Response(200, json={"status": "queued"}),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError, match="request_id") as excinfo:
        provider.submit(MODEL, {"prompt": "x"}, "k")
    assert excinfo.value.code == "invalid_submit_response"


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_code", "retryable"),
    [
        (400, "prompt inválido", "bad_request", False),
        (400, "concurrency reached, try later", "bad_request", True),
        (401, "bad key", "unauthorized", False),
        (403, "no credits", "insufficient_credits", False),
        (404, "not here", "not_found", False),
        (422, "idempotency key reused", "unprocessable", False),
        (423, "model blocked", "model_blocked", True),
        (429, "slow down", "rate_limited", True),
        (500, "boom", "server_error", True),
        (503, "unavailable", "server_error", True),
    ],
)
def test_mapeo_de_errores(
    recorder: Recorder,
    status_code: int,
    detail: str,
    expected_code: str,
    retryable: bool,
) -> None:
    recorder.add(
        "POST",
        f"https://api.higgsfield.ai/{MODEL}",
        httpx.Response(status_code, json={"detail": detail}),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError) as excinfo:
        provider.submit(MODEL, {"prompt": "x"}, "k")
    assert excinfo.value.code == expected_code
    assert excinfo.value.retryable is retryable
    assert excinfo.value.http_status == status_code


def test_error_400_con_detalle_en_lista(recorder: Recorder) -> None:
    recorder.add(
        "POST",
        f"https://api.higgsfield.ai/{MODEL}",
        httpx.Response(400, json={"detail": ["campo A", "campo B"]}),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError, match="campo A; campo B"):
        provider.submit(MODEL, {"prompt": "x"}, "k")


def test_status_completado_con_imagenes(recorder: Recorder) -> None:
    recorder.add(
        "GET",
        "https://api.higgsfield.ai/requests/req-1/status",
        httpx.Response(
            200,
            json={
                "status": "completed",
                "images": [{"url": "https://cdn/1.png"}, {"url": "https://cdn/2.png"}],
            },
        ),
    )
    provider = make_provider(recorder)
    status = provider.status("req-1")
    assert status.status == "completed"
    assert status.image_urls == ("https://cdn/1.png", "https://cdn/2.png")


def test_status_fallido_con_error(recorder: Recorder) -> None:
    recorder.add(
        "GET",
        "https://api.higgsfield.ai/requests/req-1/status",
        httpx.Response(200, json={"status": "failed", "error": "contenido bloqueado"}),
    )
    provider = make_provider(recorder)
    status = provider.status("req-1")
    assert status.status == "failed"
    assert status.error == "contenido bloqueado"


def test_status_sin_status_invalido(recorder: Recorder) -> None:
    recorder.add(
        "GET",
        "https://api.higgsfield.ai/requests/req-1/status",
        httpx.Response(200, json={"images": []}),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError, match="inválida"):
        provider.status("req-1")


def test_cancel_ok_y_404(recorder: Recorder) -> None:
    recorder.add(
        "POST",
        "https://api.higgsfield.ai/requests/req-1/cancel",
        httpx.Response(200, json={"status": "canceled"}),
    )
    provider = make_provider(recorder)
    assert provider.cancel("req-1") is True
    request = recorder.by_method_path("POST", "/requests/req-1/cancel")[0]
    assert request.headers["Authorization"] == "Key key-id:key-secret"

    recorder.add(
        "POST",
        "https://api.higgsfield.ai/requests/req-2/cancel",
        httpx.Response(404, json={"detail": "no existe"}),
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.cancel("req-2")
    assert excinfo.value.code == "not_found"
    assert excinfo.value.retryable is False


def test_estimate_ok_y_404(recorder: Recorder) -> None:
    recorder.add(
        "POST",
        f"https://api.higgsfield.ai/estimate/{MODEL}",
        httpx.Response(200, json={"credits": "1.5", "usd": "0.09"}),
    )
    provider = make_provider(recorder)
    estimate = provider.estimate(MODEL, {"prompt": "x"})
    assert estimate is not None
    assert estimate.credits == "1.5"
    assert estimate.usd == "0.09"

    recorder.add(
        "POST",
        "https://api.higgsfield.ai/estimate/otro",
        httpx.Response(404, json={"detail": "no documentado"}),
    )
    assert provider.estimate("otro", {"prompt": "x"}) is None


def test_upload_put_sin_credenciales(recorder: Recorder, tmp_path: Path) -> None:
    image = tmp_path / "foto.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontenido")

    recorder.add(
        "POST",
        "https://api.higgsfield.ai/files/generate-upload-url",
        httpx.Response(
            200,
            json={
                "upload_url": "https://storage.example/put/abc",
                "public_url": "https://cdn.example/foto.png",
                "content_type": "image/png",
                "upload_headers": {"Content-Type": "image/png"},
            },
        ),
    )
    recorder.add(
        "PUT",
        "https://storage.example/put/abc",
        httpx.Response(200),
    )
    provider = make_provider(recorder)
    public_url = provider.upload_reference(image, "image/png")
    assert public_url == "https://cdn.example/foto.png"

    auth_requests = recorder.by_method_path("POST", "/files/generate-upload-url")
    assert auth_requests[0].headers["Authorization"] == "Key key-id:key-secret"
    put = recorder.by_method_path("PUT", "/put/abc")[0]
    assert "Authorization" not in put.headers
    assert put.headers.get("Content-Type") == "image/png"
    assert put.content == b"\x89PNG\r\n\x1a\ncontenido"


def test_upload_respuesta_invalida(recorder: Recorder, tmp_path: Path) -> None:
    image = tmp_path / "foto.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nx")
    recorder.add(
        "POST",
        "https://api.higgsfield.ai/files/generate-upload-url",
        httpx.Response(200, json={"upload_url": None}),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError, match="upload_url"):
        provider.upload_reference(image, "image/png")


def test_upload_put_falla_5xx_retryable(recorder: Recorder, tmp_path: Path) -> None:
    image = tmp_path / "foto.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nx")
    recorder.add(
        "POST",
        "https://api.higgsfield.ai/files/generate-upload-url",
        httpx.Response(
            200,
            json={
                "upload_url": "https://storage.example/put/abc",
                "public_url": "https://cdn.example/f.png",
            },
        ),
    )
    recorder.add(
        "PUT",
        "https://storage.example/put/abc",
        httpx.Response(500, json={"detail": "storage caído"}),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError) as excinfo:
        provider.upload_reference(image, "image/png")
    assert excinfo.value.code == "upload_failed"
    assert excinfo.value.retryable is True


def test_download_por_content_type_y_sin_auth(
    recorder: Recorder, tmp_path: Path
) -> None:
    body = b"webp-bytes"
    recorder.handler = lambda request: httpx.Response(  # noqa: E731
        200, content=body, headers={"content-type": "image/webp; charset=binary"}
    )
    provider = make_provider(recorder)
    written = provider.download("https://cdn.example/resultado", tmp_path / "original-v1")
    assert written.name == "original-v1.webp"
    assert written.read_bytes() == body
    get = recorder.by_method_path("GET", "/resultado")[0]
    assert "Authorization" not in get.headers


def test_download_url_vacia_usa_png(
    recorder: Recorder, tmp_path: Path
) -> None:
    recorder.handler = lambda request: httpx.Response(  # noqa: E731
        200, content=b"x", headers={"content-type": "application/octet-stream"}
    )
    provider = make_provider(recorder)
    written = provider.download("https://cdn.example/archivo", tmp_path / "original-v1")
    assert written.name == "original-v1.png"


def test_download_404_mapeado(recorder: Recorder, tmp_path: Path) -> None:
    recorder.handler = lambda request: httpx.Response(  # noqa: E731
        404, json={"detail": "expirado"}
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderError) as excinfo:
        provider.download("https://cdn.example/x", tmp_path / "original-v1")
    assert excinfo.value.code == "not_found"
    assert excinfo.value.retryable is False


def test_api_key_en_blanco_rechazada() -> None:
    with pytest.raises(ProviderError, match="Falta la clave"):
        HiggsfieldProvider("   ")


def test_model_specs_igual_catalogo() -> None:
    provider = HiggsfieldProvider("k")
    try:
        ids = {spec.id for spec in provider.model_specs()}
        assert ids == {"marketing-studio/image", "marketing-studio/image/flare"}
    finally:
        provider.close()
