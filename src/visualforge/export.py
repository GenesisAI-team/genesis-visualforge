"""Exportación: nombres semánticos, optimización web (Pillow) y ZIP de lote.

Principios (GEN-11):
- Nombres tipo ``hero-exterior-v1.webp`` sin colisiones (versión por asset).
- El master original nunca se altera: la optimización crea un fichero nuevo.
- Sin upscale ficticio: solo se reduce (``max_width``) si la imagen es mayor.
- El ZIP usa rutas portables y tolera archivos faltantes o corruptos.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .models import Batch, Job, Project, ValidationError
from .storage import Store

WEB_FORMATS = {"webp": ("WEBP", ".webp"), "jpeg": ("JPEG", ".jpg"), "png": ("PNG", ".png")}


@dataclass
class ExportOptions:
    web_format: str = "webp"
    quality: int = 82  # 1-100 (webp/jpeg); para png se usa optimize
    max_width: int | None = None  # p. ej. 1920: solo reduce si es mayor

    def validate(self) -> None:
        if self.web_format not in WEB_FORMATS:
            raise ValidationError(
                f"Formato de exportación no soportado: '{self.web_format}'. "
                f"Opciones: {', '.join(sorted(WEB_FORMATS))}."
            )
        if not 1 <= self.quality <= 100:
            raise ValidationError("La calidad debe estar entre 1 y 100.")


@dataclass
class ExportResult:
    export_dir: Path
    zip_path: Path | None
    originals: list[tuple[Job, Path]] = field(default_factory=list)
    web_assets: list[tuple[Job, Path]] = field(default_factory=list)
    missing: list[Job] = field(default_factory=list)
    corrupt: list[Job] = field(default_factory=list)


def web_filename(job: Job, web_format: str) -> str:
    ext = WEB_FORMATS[web_format][1]
    return f"{job.filename_base}{ext}"


def optimize_to_web(
    src: Path,
    dest: Path,
    *,
    web_format: str = "webp",
    quality: int = 82,
    max_width: int | None = None,
) -> Path:
    """Crea una copia optimizada para web. Nunca modifica el original."""
    if web_format not in WEB_FORMATS:
        raise ValidationError(f"Formato no soportado: '{web_format}'")
    pillow_format, ext = WEB_FORMATS[web_format]
    if dest.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        # "hero-exterior-v1" no es una extensión: se anexa la correcta.
        dest = Path(f"{dest}{ext}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as image:
        image.load()
        working = image

        if max_width and working.width > max_width:
            ratio = max_width / working.width
            new_height = max(1, round(working.height * ratio))
            working = working.resize((max_width, new_height), Image.LANCZOS)

        if pillow_format == "JPEG":
            if working.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", working.size, (255, 255, 255))
                rgba = working.convert("RGBA")
                background.paste(rgba, mask=rgba.getchannel("A"))
                working = background
            else:
                working = working.convert("RGB")
            working.save(dest, format=pillow_format, quality=quality, optimize=True)
        elif pillow_format == "WEBP":
            if working.mode not in ("RGB", "RGBA"):
                working = working.convert("RGBA" if "A" in working.getbands() else "RGB")
            working.save(dest, format=pillow_format, quality=quality, method=4)
        else:
            working.save(dest, format=pillow_format, optimize=True)

    return dest


def export_batch(
    store: Store,
    project: Project,
    batch: Batch,
    jobs: list[Job],
    options: ExportOptions,
    *,
    make_zip: bool = True,
) -> ExportResult:
    """Exporta un lote a ``projects/<slug>/exports/`` (+ ZIP)."""
    options.validate()

    export_dir = store.project_dir(project.slug) / "exports"
    originals_dir = export_dir / "originals"
    web_dir = export_dir / "web"
    for path in (originals_dir, web_dir):
        path.mkdir(parents=True, exist_ok=True)

    result = ExportResult(export_dir=export_dir, zip_path=None)
    completed = [job for job in jobs if job.original_path]

    for job in completed:
        src = store.project_dir(project.slug) / job.original_path
        if not src.is_file():
            result.missing.append(job)
            continue

        suffix = src.suffix or ".png"
        original_dest = originals_dir / f"{job.filename_base}{suffix}"
        try:
            shutil.copy2(src, original_dest)
            result.originals.append((job, original_dest))
        except OSError:
            result.missing.append(job)
            continue

        web_dest = web_dir / web_filename(job, options.web_format)
        try:
            optimize_to_web(
                src,
                web_dest,
                web_format=options.web_format,
                quality=options.quality,
                max_width=options.max_width,
            )
            result.web_assets.append((job, web_dest))
        except (UnidentifiedImageError, OSError, ValueError):
            result.corrupt.append(job)

    # Prompts y manifiesto junto a los assets (trazabilidad del paquete)
    prompts_file = store.project_dir(project.slug) / "prompts.json"
    if prompts_file.is_file():
        shutil.copy2(prompts_file, export_dir / "prompts.json")
    manifest_path = store.write_batch_manifest(batch, jobs)
    shutil.copy2(manifest_path, export_dir / "manifest.json")

    _write_readme(export_dir, project, batch, jobs, result, options)

    if make_zip:
        zip_path = export_dir / f"{project.slug}-{batch.id[:8]}.zip"
        result.zip_path = _make_zip(export_dir, zip_path)
    return result


def _write_readme(
    export_dir: Path,
    project: Project,
    batch: Batch,
    jobs: list[Job],
    result: ExportResult,
    options: ExportOptions,
) -> None:
    lines = [
        f"# Assets — {project.name} (lote {batch.id[:8]})",
        "",
        f"Modelo: `{batch.config.model}` · Resolución: {batch.config.resolution} · "
        f"Ratio: {batch.config.aspect_ratio} · Formato web: {options.web_format} "
        f"(calidad {options.quality}).",
        "",
        "| Asset | Versión | Estado | Archivo web | Archivo original |",
        "|---|---|---|---|---|",
    ]
    web_by_job = {job.id: path.name for job, path in result.web_assets}
    original_by_job = {job.id: path.name for job, path in result.originals}
    for job in jobs:
        web_name = web_by_job.get(job.id, "—")
        original_name = original_by_job.get(job.id, "—")
        status = str(job.status)
        if job.id in {j.id for j in result.missing}:
            status += " (faltante en disco)"
        if job.id in {j.id for j in result.corrupt}:
            status += " (imagen corrupta, omitida)"
        lines.append(
            f"| {job.asset_name} | v{job.version} | {status} | {web_name} | {original_name} |"
        )

    lines += [
        "",
        "Notas:",
        "- Los originales en `originals/` no se modifican; `web/` son copias optimizadas.",
        "- Este paquete no contiene claves de API ni URLs firmadas de subida.",
        "- Las imágenes generadas son interpretación del prompt, no reproducción "
        "arquitectónica exacta del local de referencia.",
        "",
    ]
    (export_dir / "README-assets.md").write_text("\n".join(lines), encoding="utf-8")


def _make_zip(export_dir: Path, zip_path: Path) -> Path:
    """ZIP con rutas relativas (portables) y tolerancia a errores por archivo."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(export_dir.rglob("*")):
            if path == zip_path or not path.is_file():
                continue
            relative = path.relative_to(export_dir).as_posix()
            try:
                archive.write(path, arcname=relative)
            except OSError:
                continue
    return zip_path
