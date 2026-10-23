"""Proveedor mock: idempotencia, reintentos, cancelación y descarga."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from visualforge.models import new_id
from visualforge.provider.base import ProviderError
from visualforge.provider.mock import MockProvider

MODEL = "marketing-studio/image/flare"


def _poll_until_terminal(provider: MockProvider, request_id: str):
    status = provider.status(request_id)
    while status.status not in {"completed", "failed", "nsfw", "canceled"}:
        status = provider.status(request_id)
    return status


def test_submit_idempotente_reutiliza_request_id() -> None:
    provider = MockProvider()
    payload = {"prompt": "hola"}
    first = provider.submit(MODEL, payload, "key-1")
    second = provider.submit(MODEL, payload, "key-1")
    assert first.request_id == second.request_id
    assert provider.submit_calls == 2
    assert len(provider.charged_keys) == 1


def test_submit_keys_distintas_generan_ids_distintos() -> None:
    provider = MockProvider()
    payload = {"prompt": "hola"}
    a = provider.submit(MODEL, payload, "key-a")
    b = provider.submit(MODEL, payload, "key-b")
    assert a.request_id != b.request_id
    assert len(provider.charged_keys) == 2


def test_submit_failures_configurables() -> None:
    provider = MockProvider()
    provider.scenarios.submit_failures = 2
    payload = {"prompt": "hola"}
    with pytest.raises(ProviderError) as first:
        provider.submit(MODEL, payload, new_id())
    assert first.value.retryable is True
    with pytest.raises(ProviderError):
        provider.submit(MODEL, payload, new_id())
    result = provider.submit(MODEL, payload, new_id())
    assert result.request_id


def test_reject_submits() -> None:
    provider = MockProvider()
    provider.scenarios.reject_submits = True
    with pytest.raises(ProviderError) as excinfo:
        provider.submit(MODEL, {"prompt": "x"}, new_id())
    assert excinfo.value.retryable is False
    assert excinfo.value.code == "unauthorized"


def test_status_poll_progressivo() -> None:
    provider = MockProvider()
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    assert provider.status(result.request_id).status == "queued"
    # polls_to_complete=2: la segunda consulta ya resuelve el terminal.
    status = provider.status(result.request_id)
    assert status.status == "completed"
    assert status.image_urls


def test_status_failures_inyectables() -> None:
    provider = MockProvider()
    provider.scenarios.status_failures = 1
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    with pytest.raises(ProviderError):
        provider.status(result.request_id)
    assert provider.status(result.request_id).status in {"queued", "running"}


def test_status_terminal_failed() -> None:
    provider = MockProvider()
    provider.scenarios.fail_terminal = True
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    status = _poll_until_terminal(provider, result.request_id)
    assert status.status == "failed"
    assert status.error


def test_status_terminal_nsfw() -> None:
    provider = MockProvider()
    provider.scenarios.nsfw_terminal = True
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    status = _poll_until_terminal(provider, result.request_id)
    assert status.status == "nsfw"
    assert len(provider.charged_keys) == 1  # el proveedor real no cobra nsfw


def test_cancel_pending() -> None:
    provider = MockProvider()
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    assert provider.cancel(result.request_id) is True
    assert provider.status(result.request_id).status == "canceled"


def test_cancel_after_done_raises() -> None:
    provider = MockProvider()
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    _poll_until_terminal(provider, result.request_id)
    with pytest.raises(ProviderError) as excinfo:
        provider.cancel(result.request_id)
    assert excinfo.value.code == "not_cancelable"


def test_download_escribe_png_valido(tmp_path: Path) -> None:
    provider = MockProvider()
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    status = _poll_until_terminal(provider, result.request_id)
    written = provider.download(status.image_urls[0], tmp_path / "original-v1")
    assert written.name == "original-v1.png"
    with Image.open(written) as image:
        assert image.format == "PNG"
        assert image.size == (768, 512)


def test_download_url_desconocida() -> None:
    provider = MockProvider()
    with pytest.raises(ProviderError, match="no soportada"):
        provider.download("https://ejemplo.com/x.png", Path("out/original-v1"))


def test_download_404_retryable() -> None:
    provider = MockProvider()
    provider.scenarios.download_failures = 10
    result = provider.submit(MODEL, {"prompt": "x"}, new_id())
    status = _poll_until_terminal(provider, result.request_id)
    with pytest.raises(ProviderError) as excinfo:
        provider.download(status.image_urls[0], Path("x/original-v1"))
    assert excinfo.value.retryable is True
    assert excinfo.value.code == "network"


def test_estimate() -> None:
    provider = MockProvider()
    estimate = provider.estimate(MODEL, {"prompt": "x"})
    assert estimate is not None
    assert estimate.credits is not None
    assert estimate.has_value
    provider.scenarios.estimate_available = False
    assert provider.estimate(MODEL, {"prompt": "x"}) is None


def test_upload_reference(tmp_path: Path) -> None:
    provider = MockProvider()
    missing = tmp_path / "no.png"
    with pytest.raises(ProviderError, match="no encontrada"):
        provider.upload_reference(missing, "image/png")
