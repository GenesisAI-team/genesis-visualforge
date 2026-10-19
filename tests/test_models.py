"""Validación de payloads contra los esquemas documentados de los modelos."""

from __future__ import annotations

import pytest

from visualforge.models import (
    DEFAULT_MODEL_ID,
    MODELS,
    ValidationError,
    build_generation_payload,
    get_model_spec,
)


def test_catalogo_contiene_los_dos_modelos_documentados() -> None:
    assert set(MODELS) == {"marketing-studio/image", "marketing-studio/image/flare"}


def test_get_model_spec_desconocido_no_sustituye() -> None:
    with pytest.raises(ValidationError, match="no disponible"):
        get_model_spec("modelo-inexistente")


def test_flare_acepta_quality_y_las_tres_resoluciones() -> None:
    spec = get_model_spec("marketing-studio/image/flare")
    payload = build_generation_payload(
        spec, "un prompt", resolution="4k", aspect_ratio="21:9", quality="max"
    )
    assert payload["quality"] == "max"
    assert payload["resolution"] == "4k"
    assert payload["aspect_ratio"] == "21:9"
    assert payload["enhance_prompt"] is False


def test_marketing_image_no_envia_quality() -> None:
    spec = get_model_spec("marketing-studio/image")
    payload = build_generation_payload(
        spec, "un prompt", resolution="2k", aspect_ratio="16:9", quality=None
    )
    assert "quality" not in payload


def test_marketing_image_rechaza_quality_si_lo_intentas() -> None:
    spec = get_model_spec("marketing-studio/image")
    with pytest.raises(ValidationError, match="no documenta el parámetro 'quality'"):
        build_generation_payload(
            spec, "un prompt", resolution="2k", aspect_ratio="16:9", quality="high"
        )


def test_ratio_no_soportado_rechazado() -> None:
    spec = get_model_spec(DEFAULT_MODEL_ID)
    with pytest.raises(ValidationError, match="Relación de aspecto"):
        build_generation_payload(
            spec, "un prompt", resolution="2k", aspect_ratio="7:5", quality="high"
        )


def test_flare_limita_16_referencias() -> None:
    spec = get_model_spec("marketing-studio/image/flare")
    urls = [f"https://cdn.example/{i}.png" for i in range(17)]
    with pytest.raises(ValidationError, match="16 imágenes"):
        build_generation_payload(
            spec, "un prompt", resolution="2k", aspect_ratio="16:9",
            quality="high", image_urls=urls,
        )
    ok = build_generation_payload(
        spec, "un prompt", resolution="2k", aspect_ratio="16:9",
        quality="high", image_urls=urls[:16],
    )
    assert len(ok["image_urls"]) == 16


def test_prompt_vacio_rechazado() -> None:
    spec = get_model_spec(DEFAULT_MODEL_ID)
    with pytest.raises(ValidationError, match="prompt final"):
        build_generation_payload(spec, "   ", resolution="2k", aspect_ratio="auto")


def test_image_urls_ausente_si_no_hay_referencias() -> None:
    spec = get_model_spec(DEFAULT_MODEL_ID)
    payload = build_generation_payload(
        spec, "prompt", resolution="1k", aspect_ratio="1:1", quality="low"
    )
    assert "image_urls" not in payload
