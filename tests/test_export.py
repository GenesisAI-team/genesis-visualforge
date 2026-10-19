"""Exportación: nombres semánticos, optimización web y ZIP de lote."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from PIL import Image

from visualforge.export import (
    ExportOptions,
    export_batch,
    optimize_to_web,
    web_filename,
)
from visualforge.models import Job, ValidationError
from visualforge.storage import Store


def test_web_filename() -> None:
    job = Job(
        id="j",
        batch_id="b",
        project_slug="p",
        prompt_id="x",
        asset_name="Hero",
        slug="hero-exterior",
        version=1,
        model="m",
        final_prompt="f",
        payload={},
        reference_ids=[],
        idempotency_key="k",
    )
    assert web_filename(job, "webp") == "hero-exterior-v1.webp"
    assert web_filename(job, "jpeg") == "hero-exterior-v1.jpg"


def test_optimize_no_hace_upscale(tmp_path: Path) -> None:
    src = tmp_path / "small.png"
    Image.new("RGB", (100, 50), (10, 20, 30)).save(src)
    dest = optimize_to_web(src, tmp_path / "out", max_width=1920)
    with Image.open(dest) as image:
        assert image.size == (100, 50)
        assert image.format == "WEBP"


def test_optimize_reduce_si_es_mayor(tmp_path: Path) -> None:
    src = tmp_path / "big.png"
    Image.new("RGB", (768, 512), (1, 2, 3)).save(src)
    dest = optimize_to_web(src, tmp_path / "out", max_width=300)
    with Image.open(dest) as image:
        assert image.size == (300, 200)


def test_optimize_jpeg_desde_rgba(tmp_path: Path) -> None:
    src = tmp_path / "alpha.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(src)
    dest = optimize_to_web(src, tmp_path / "out", web_format="jpeg")
    with Image.open(dest) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"


def test_optimize_no_toca_el_original(tmp_path: Path) -> None:
    src = tmp_path / "master.png"
    Image.new("RGB", (64, 64), (9, 9, 9)).save(src)
    before = src.read_bytes()
    optimize_to_web(src, tmp_path / "web" / "copia")
    assert src.read_bytes() == before


def test_optimize_formato_invalido(tmp_path: Path) -> None:
    src = tmp_path / "x.png"
    Image.new("RGB", (8, 8)).save(src)
    with pytest.raises(ValidationError, match="no soportado"):
        optimize_to_web(src, tmp_path / "out", web_format="tiff")


def test_export_batch_completo(
    store: Store, completed_batch, project
) -> None:
    batch, jobs = completed_batch
    result = export_batch(store, project, batch, jobs, ExportOptions())

    assert result.missing == []
    assert result.corrupt == []
    assert len(result.originals) == 3
    assert len(result.web_assets) == 3

    for job, web_path in result.web_assets:
        assert web_path.name == web_filename(job, "webp")
        assert web_path.is_file()

    export_dir = result.export_dir
    assert (export_dir / "README-assets.md").is_file()
    assert (export_dir / "manifest.json").is_file()
    assert (export_dir / "prompts.json").is_file()
    assert result.zip_path is not None and result.zip_path.is_file()

    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    assert "README-assets.md" in names
    assert "manifest.json" in names
    assert "prompts.json" in names
    for job in jobs:
        assert f"web/{web_filename(job, 'webp')}" in names
        assert f"originals/{job.filename_base}.png" in names


def test_export_calidad_y_formato(
    store: Store, completed_batch, project
) -> None:
    batch, jobs = completed_batch
    result = export_batch(
        store,
        project,
        batch,
        jobs,
        ExportOptions(web_format="jpeg", quality=70),
    )
    for _, path in result.web_assets:
        with Image.open(path) as image:
            assert image.format == "JPEG"


def test_export_tolera_original_faltante(
    store: Store, completed_batch, project
) -> None:
    batch, jobs = completed_batch
    victim = jobs[0]
    assert victim.original_path is not None
    (store.project_dir(project.slug) / victim.original_path).unlink()

    result = export_batch(store, project, batch, jobs, ExportOptions())
    assert [j.id for j in result.missing] == [victim.id]
    assert len(result.web_assets) == 2
    readme = (result.export_dir / "README-assets.md").read_text(encoding="utf-8")
    assert "faltante en disco" in readme
    assert result.zip_path is not None and result.zip_path.is_file()


def test_export_tolera_imagen_corrupta(
    store: Store, completed_batch, project
) -> None:
    batch, jobs = completed_batch
    victim = jobs[0]
    assert victim.original_path is not None
    original = store.project_dir(project.slug) / victim.original_path
    original.write_bytes(b"esto no es un png")

    result = export_batch(store, project, batch, jobs, ExportOptions())
    assert [j.id for j in result.corrupt] == [victim.id]
    # El original corrupto sí se copia (copia byte a byte), pero no hay web.
    assert len(result.originals) == 3
    assert len(result.web_assets) == 2
    readme = (result.export_dir / "README-assets.md").read_text(encoding="utf-8")
    assert "corrupta" in readme


def test_export_sin_zip(store: Store, completed_batch, project) -> None:
    batch, jobs = completed_batch
    result = export_batch(
        store, project, batch, jobs, ExportOptions(), make_zip=False
    )
    assert result.zip_path is None


def test_export_options_invalidas() -> None:
    with pytest.raises(ValidationError, match="Formato"):
        ExportOptions(web_format="bmp").validate()
    with pytest.raises(ValidationError, match="calidad"):
        ExportOptions(quality=0).validate()


def test_export_incluye_solo_completados(
    store: Store, completed_batch, project
) -> None:
    batch, jobs = completed_batch
    # Marcar uno como fallido sin original: no debe exportarse su web.
    failed = jobs[1]
    store.update_job(failed.id, original_path=None)
    result = export_batch(store, project, batch, store.get_jobs(batch.id), ExportOptions())
    exported_ids = {job.id for job, _ in result.web_assets}
    assert failed.id not in exported_ids
    assert len(result.web_assets) == 2
