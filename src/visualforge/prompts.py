"""Utilidades de prompts: slugs, importación y composición del prompt final.

Formato de importación «uno por línea»:
  - Línea normal  -> prompt con nombre automático (asset-1, asset-2, ...).
  - ``Nombre :: prompt`` -> con nombre semántico (separador doble, sin ambigüedades).

El prompt final enviado = dirección de arte del prompt base + prompt del asset
+ campo «mantener». Cada job guarda este prompt final exacto.
"""

from __future__ import annotations

import re
import unicodedata

from .models import PromptEntry, ValidationError, new_id

_NAME_SEPARATOR = "::"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_MULTI_DASH = re.compile(r"-{2,}")


def slugify(text: str) -> str:
    """Nombre de archivo seguro y semántico: 'Hero exterior' -> 'hero-exterior'."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_text).strip("-")
    slug = _MULTI_DASH.sub("-", slug)
    return slug or "asset"


def parse_prompts_text(text: str, start_index: int = 1) -> list[PromptEntry]:
    """Interpreta el texto del importador y devuelve entradas de prompt.

    Líneas vacías se ignoran; los slugs se desdoblan ante colisiones.
    """
    entries: list[PromptEntry] = []
    used_slugs: set[str] = set()
    auto_index = start_index

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if _NAME_SEPARATOR in line:
            name, _, prompt = line.partition(_NAME_SEPARATOR)
            name = name.strip()
            prompt = prompt.strip()
        else:
            name = f"Asset {auto_index}"
            prompt = line
            auto_index += 1

        if not prompt:
            raise ValidationError(f"Línea sin prompt: «{line}»")

        base_slug = slugify(name)
        slug = base_slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        used_slugs.add(slug)

        entries.append(
            PromptEntry(id=new_id(), name=name or slug, slug=slug, prompt=prompt)
        )

    if not entries:
        raise ValidationError("No se ha detectado ningún prompt.")
    return entries


def ensure_unique_slug(slug: str, used: set[str]) -> str:
    candidate = slug or "asset"
    suffix = 2
    while candidate in used:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def compose_final_prompt(art_direction: str, entry: PromptEntry) -> str:
    """Prompt final exacto que se enviará al proveedor (se guarda con el job)."""
    parts: list[str] = []
    base = (art_direction or "").strip()
    prompt = entry.prompt.strip()
    keep = (entry.keep or "").strip()

    if base:
        parts.append(base)
    if prompt:
        parts.append(prompt)
    if keep:
        parts.append(f"Mantener sin cambios: {keep}")

    final = "\n\n".join(parts).strip()
    if not final:
        raise ValidationError(
            f"El prompt de «{entry.name}» está vacío y no hay dirección de arte."
        )
    return final
