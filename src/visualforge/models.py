"""Modelos de dominio tipados de Genesis VisualForge.

Todo lo referente a proyectos, referencias, prompts, lotes y jobs.
La validación de payloads contra el esquema real de cada modelo vive aquí:
nunca se muestran ni envían opciones que el modelo no documenta.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Estados de job (alineados con la API de Higgsfield + estados locales)
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    PENDING = "pending"      # planificado, aún no enviado
    QUEUED = "queued"        # en cola local, a punto de enviarse
    RUNNING = "running"      # request_id emitido, en polling
    COMPLETED = "completed"  # resultado descargado a disco
    FAILED = "failed"        # error (submit, proveedor o descarga)
    CANCELED = "canceled"    # cancelado por el usuario


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED})

# Mapeo de estados terminales de la API remota -> estado local
REMOTE_TERMINAL_MAP = {
    "completed": JobStatus.COMPLETED,
    "failed": JobStatus.FAILED,
    "nsfw": JobStatus.FAILED,
    "canceled": JobStatus.CANCELED,
}

# ---------------------------------------------------------------------------
# Restricciones locales de referencias (MIME soportado por la API de Higgsfield)
# ---------------------------------------------------------------------------

ALLOWED_REFERENCE_MIME = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
)

# Límite local de sanidad (la API documenta MIME pero no un máximo de tamaño).
MAX_REFERENCE_MB_DEFAULT = 25


class ValidationError(ValueError):
    """Error de validación de entradas del usuario o payload."""


# ---------------------------------------------------------------------------
# Especificaciones de modelos (verificadas en la documentación oficial)
# ---------------------------------------------------------------------------

ASPECT_RATIOS = ("auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9")
RESOLUTIONS = ("1k", "2k", "4k")
FLARE_QUALITIES = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelSpec:
    """Descripción verificada de un modelo documentado de Higgsfield."""

    id: str
    label: str
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    qualities: tuple[str, ...] | None  # None -> el modelo no documenta "quality"
    max_references: int | None  # None -> sin máximo documentado
    description: str = ""

    def validate_options(
        self,
        resolution: str,
        aspect_ratio: str,
        quality: str | None,
    ) -> None:
        if resolution not in self.resolutions:
            raise ValidationError(
                f"Resolución '{resolution}' no soportada por {self.label}. "
                f"Opciones: {', '.join(self.resolutions)}."
            )
        if aspect_ratio not in self.aspect_ratios:
            raise ValidationError(
                f"Relación de aspecto '{aspect_ratio}' no soportada por {self.label}. "
                f"Opciones: {', '.join(self.aspect_ratios)}."
            )
        if quality is not None:
            if self.qualities is None:
                raise ValidationError(
                    f"{self.label} no documenta el parámetro 'quality'; no se enviará."
                )
            if quality not in self.qualities:
                raise ValidationError(
                    f"Calidad '{quality}' no soportada por {self.label}. "
                    f"Opciones: {', '.join(self.qualities)}."
                )

    def validate_references(self, image_urls: list[str]) -> None:
        if self.max_references is not None and len(image_urls) > self.max_references:
            raise ValidationError(
                f"{self.label} admite como máximo {self.max_references} imágenes de "
                f"referencia; se han asignado {len(image_urls)}."
            )


# Catálogo verificado (2026-10-19) en:
# - https://open.higgsfield.ai/models/marketing-studio/image/api-reference
# - https://open.higgsfield.ai/models/marketing-studio/image/flare/api-reference
MODELS: dict[str, ModelSpec] = {
    "marketing-studio/image": ModelSpec(
        id="marketing-studio/image",
        label="Marketing Studio Image 2.0",
        resolutions=RESOLUTIONS,
        aspect_ratios=ASPECT_RATIOS,
        qualities=None,  # sin "quality" documentado en 2.0
        max_references=None,  # documentado "one or more", sin máximo publicado
        description="Campañas y edición de imágenes hasta 4K (2.0 Alpha).",
    ),
    "marketing-studio/image/flare": ModelSpec(
        id="marketing-studio/image/flare",
        label="Marketing Studio Image 2.5 Flare",
        resolutions=RESOLUTIONS,
        aspect_ratios=ASPECT_RATIOS,
        qualities=FLARE_QUALITIES,
        max_references=16,  # "up to 16" image_urls
        description="Flare: calidad ajustable (low..max) y hasta 16 referencias.",
    ),
}

DEFAULT_MODEL_ID = "marketing-studio/image/flare"


def get_model_spec(model_id: str) -> ModelSpec:
    spec = MODELS.get(model_id)
    if spec is None:
        # Nunca sustituir el modelo pedido por otro: fallo explícito.
        raise ValidationError(
            f"Modelo '{model_id}' no disponible en el catálogo. "
            f"Modelos documentados: {', '.join(MODELS)}."
        )
    return spec


def build_generation_payload(
    spec: ModelSpec,
    final_prompt: str,
    resolution: str,
    aspect_ratio: str,
    quality: str | None = None,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Valida contra el esquema del modelo y construye el payload a enviar."""
    if not final_prompt or not final_prompt.strip():
        raise ValidationError("El prompt final no puede estar vacío.")
    spec.validate_options(resolution, aspect_ratio, quality)

    urls = list(image_urls or [])
    spec.validate_references(urls)

    payload: dict[str, Any] = {
        "prompt": final_prompt.strip(),
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "enhance_prompt": False,
    }
    if spec.qualities is not None and quality is not None:
        payload["quality"] = quality
    if urls:
        payload["image_urls"] = urls
    return payload


# ---------------------------------------------------------------------------
# Entidades
# ---------------------------------------------------------------------------


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ReferenceImage:
    """Fotografía de referencia subida al proyecto (archivo local)."""

    id: str
    filename: str  # nombre original saneado
    path: str  # relativo al directorio del proyecto
    mime: str
    size_bytes: int
    uploaded_at: float = field(default_factory=time.time)
    public_url: str | None = None  # URL tras subirla al proveedor (cacheada)
    uploaded_at_remote: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "path": self.path,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "uploaded_at": self.uploaded_at,
            "public_url": self.public_url,
            "uploaded_at_remote": self.uploaded_at_remote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReferenceImage:
        return cls(**data)


@dataclass
class PromptEntry:
    """Un asset del lote: nombre semántico + prompt + referencias asignadas."""

    id: str
    name: str  # nombre visible, p. ej. "Hero exterior"
    slug: str  # nombre de archivo, p. ej. "hero-exterior"
    prompt: str
    keep: str = ""  # campo «mantener»: elementos que no deben cambiar
    reference_ids: list[str] = field(default_factory=list)
    resolution: str | None = None  # override por imagen (None -> global)
    aspect_ratio: str | None = None
    quality: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "prompt": self.prompt,
            "keep": self.keep,
            "reference_ids": list(self.reference_ids),
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "quality": self.quality,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptEntry:
        return cls(**data)


@dataclass
class Project:
    slug: str
    name: str
    brief: str = ""
    art_direction: str = ""  # prompt base de dirección de arte
    created_at: float = field(default_factory=time.time)
    references: list[ReferenceImage] = field(default_factory=list)
    prompts: list[PromptEntry] = field(default_factory=list)

    def reference_by_id(self, ref_id: str) -> ReferenceImage | None:
        for ref in self.references:
            if ref.id == ref_id:
                return ref
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "brief": self.brief,
            "art_direction": self.art_direction,
            "created_at": self.created_at,
            "references": [r.to_dict() for r in self.references],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], prompts: list[PromptEntry]) -> Project:
        project = cls(
            slug=data["slug"],
            name=data["name"],
            brief=data.get("brief", ""),
            art_direction=data.get("art_direction", ""),
            created_at=data.get("created_at", time.time()),
            references=[ReferenceImage.from_dict(r) for r in data.get("references", [])],
            prompts=prompts,
        )
        return project


@dataclass
class BatchConfig:
    model: str
    resolution: str
    aspect_ratio: str
    quality: str | None = None
    concurrency: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "quality": self.quality,
            "concurrency": self.concurrency,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchConfig:
        return cls(**data)


BATCH_PENDING = "pending"
BATCH_RUNNING = "running"
BATCH_FINISHED = "finished"


@dataclass
class Batch:
    id: str
    project_slug: str
    config: BatchConfig
    status: str = BATCH_PENDING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    regenerates_batch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_slug": self.project_slug,
            "config": self.config.to_dict(),
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "regenerates_batch": self.regenerates_batch,
        }


@dataclass
class Job:
    """Una generación individual: un prompt -> una imagen."""

    id: str
    batch_id: str
    project_slug: str
    prompt_id: str
    asset_name: str
    slug: str
    version: int
    model: str
    final_prompt: str
    payload: dict[str, Any]
    reference_ids: list[str]
    idempotency_key: str  # persistida ANTES del primer envío
    status: JobStatus = JobStatus.PENDING
    request_id: str | None = None
    attempts: int = 0
    error: str | None = None
    estimate_credits: str | None = None
    estimate_usd: str | None = None
    result_url: str | None = None
    original_path: str | None = None  # relativo al directorio del proyecto
    web_path: str | None = None
    regenerates: str | None = None  # id del job que regenera
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def filename_base(self) -> str:
        return f"{self.slug}-v{self.version}"


def make_job(
    *,
    batch_id: str,
    project_slug: str,
    entry: PromptEntry,
    version: int,
    model: str,
    final_prompt: str,
    payload: dict[str, Any],
    regenerates: str | None = None,
) -> Job:
    """Crea un job con su idempotency_key única (clave por intención de generación)."""
    now = time.time()
    return Job(
        id=new_id(),
        batch_id=batch_id,
        project_slug=project_slug,
        prompt_id=entry.id,
        asset_name=entry.name,
        slug=entry.slug,
        version=version,
        model=model,
        final_prompt=final_prompt,
        payload=payload,
        reference_ids=list(entry.reference_ids),
        idempotency_key=str(uuid.uuid4()),
        regenerates=regenerates,
        created_at=now,
        updated_at=now,
    )
