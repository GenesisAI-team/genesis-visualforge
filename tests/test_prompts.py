"""Utilidades de prompts: slugs, importación y prompt final compuesto."""

from __future__ import annotations

import pytest

from visualforge.models import PromptEntry, ValidationError, new_id
from visualforge.prompts import (
    compose_final_prompt,
    ensure_unique_slug,
    parse_prompts_text,
    slugify,
)


def test_slugify_con_acentos_y_espacios() -> None:
    # "º" (NFKD) -> "o"; acentos se reducen a ASCII.
    assert slugify("Terraza Exterior Nº 1") == "terraza-exterior-no-1"
    assert slugify("  HERO  Extérieur!! ") == "hero-exterieur"


def test_parse_un_prompt_por_linea_con_nombre_automatico() -> None:
    entries = parse_prompts_text("primera foto de portada\nsegunda foto de fachada")
    assert len(entries) == 2
    assert entries[0].name == "Asset 1"
    assert entries[1].prompt == "segunda foto de fachada"
    assert entries[0].slug != entries[1].slug


def test_parse_con_separador_doble_colon() -> None:
    entries = parse_prompts_text("Hero :: terraza al atardecer\nFachada :: frontal")
    assert [e.name for e in entries] == ["Hero", "Fachada"]
    assert entries[0].slug == "hero"
    assert entries[1].prompt == "frontal"


def test_parse_colisiones_de_slug_se_desdoblan() -> None:
    entries = parse_prompts_text("Hero :: uno\nHero :: dos")
    assert entries[0].slug == "hero"
    assert entries[1].slug == "hero-2"


def test_parse_vacio_lanza_error() -> None:
    with pytest.raises(ValidationError, match="ningún prompt"):
        parse_prompts_text("  \n \n ")


def test_ensure_unique_slug() -> None:
    used = {"hero"}
    assert ensure_unique_slug("hero", used) == "hero-2"
    assert ensure_unique_slug("terraza", used) == "terraza"


def _entry(**kwargs) -> PromptEntry:
    defaults = dict(id=new_id(), name="Hero", slug="hero", prompt="foto principal")
    defaults.update(kwargs)
    return PromptEntry(**defaults)


def test_compose_con_direccion_de_arte_y_mantener() -> None:
    entry = _entry(keep="el toldo y el lema")
    final = compose_final_prompt("Paleta cálida, luz suave.", entry)
    assert final.startswith("Paleta cálida, luz suave.")
    assert "foto principal" in final
    assert "Mantener sin cambios: el toldo y el lema" in final


def test_compose_sin_direccion_de_arte() -> None:
    final = compose_final_prompt("", _entry())
    assert final == "foto principal"


def test_compose_todo_vacio_lanza_error() -> None:
    with pytest.raises(ValidationError):
        compose_final_prompt("", _entry(prompt="   "))
