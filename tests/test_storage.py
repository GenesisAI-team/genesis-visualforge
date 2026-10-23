"""Persistencia: proyectos, referencias, prompts y SQLite (jobs + batches)."""

from __future__ import annotations

import sqlite3

import pytest

from visualforge.models import (
    Batch,
    BatchConfig,
    Job,
    JobStatus,
    PromptEntry,
    ValidationError,
    make_job,
    new_id,
)
from visualforge.storage import Store, sanitize_filename


def test_project_roundtrip(store: Store) -> None:
    from tests.conftest import make_png_bytes

    from visualforge.models import Project

    demo = Project(slug="restaurante-demo", name="Restaurante Demo")
    store.save_project(demo)
    ref = store.add_reference(demo, "terraza.png", make_png_bytes(), "image/png")
    demo.prompts = [PromptEntry(id=new_id(), name="Hero", slug="hero", prompt="p")]
    store.save_project(demo)

    loaded = store.load_project("restaurante-demo")
    assert loaded.name == "Restaurante Demo"
    assert len(loaded.references) == 1
    assert loaded.references[0].id == ref.id
    assert len(loaded.prompts) == 1


def test_load_project_not_found(store: Store) -> None:
    with pytest.raises(ValidationError, match="no encontrado"):
        store.load_project("no-existe")


def test_project_dir_rejects_bad_slug(store: Store) -> None:
    with pytest.raises(ValidationError, match="Slug"):
        store.project_dir("../fuera")
    with pytest.raises(ValidationError, match="Slug"):
        store.project_dir("a/b")


def test_list_projects_skips_corrupt(store: Store, project) -> None:
    broken = store.project_dir("roto") / "project.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{ no es json", encoding="utf-8")
    names = [p.slug for p in store.list_projects()]
    assert names == ["restaurante-demo"]


def test_reference_saved_with_unique_stored_name(store: Store, project) -> None:
    from tests.conftest import make_png_bytes

    ref = store.add_reference(project, "terraza.png", make_png_bytes(), "image/png")
    path = store.reference_path(project, ref.path)
    assert path.is_file()
    assert ref.filename == "terraza.png"
    assert ref.path.startswith("references/")
    assert ref.size_bytes > 0


def test_reference_oversized_rejected(store: Store, project) -> None:
    # store del fixture limita a 1 MB: se rechaza, no se traga ni "recorta".
    with pytest.raises(ValidationError, match="MB"):
        store.add_reference(project, "grande.png", b"x" * (1024 * 1024 + 1), "image/png")


def test_reference_bad_extension_rejected(store: Store, project) -> None:
    with pytest.raises(ValidationError, match="Formato"):
        store.add_reference(
            project, "malware.exe", b"\x00\x01", "application/x-msdownload"
        )


def test_reference_nombres_duplicados_coexisten(store: Store, project) -> None:
    from tests.conftest import make_png_bytes

    a = store.add_reference(project, "terraza.png", make_png_bytes(), "image/png")
    b = store.add_reference(project, "terraza.png", make_png_bytes((1, 2, 3)), "image/png")
    assert a.id != b.id
    assert a.path != b.path  # stored_name lleva prefijo de id
    assert store.reference_path(project, a.path).is_file()
    assert store.reference_path(project, b.path).is_file()


def test_sanitize_filename() -> None:
    assert sanitize_filename("  foto (1).jpg  ") == "foto _1_.jpg"
    assert sanitize_filename("../../etc/passwd") == "passwd"
    with pytest.raises(ValidationError):
        sanitize_filename("   .  ")


_SEED_COUNTER = {"n": 0}


def _seed_job(store: Store, **overrides) -> tuple[Batch, Job]:
    _SEED_COUNTER["n"] += 1
    n = _SEED_COUNTER["n"]
    batch = Batch(
        id=new_id(),
        project_slug="demo",
        config=BatchConfig(
            model="marketing-studio/image", resolution="2k", aspect_ratio="16:9"
        ),
    )
    entry = PromptEntry(id=new_id(), name=f"Hero {n}", slug=f"hero-{n}", prompt="p")
    job = make_job(
        batch_id=batch.id,
        project_slug="demo",
        entry=entry,
        version=1,
        model="marketing-studio/image",
        final_prompt="p",
        payload={"prompt": "p"},
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    store.create_batch(batch, [job])
    return batch, job


def test_update_job_if_unsubmitted(store: Store) -> None:
    _, job = _seed_job(store)
    assert (
        store.update_job_if_unsubmitted(
            job.id, request_id="req-1", status=JobStatus.RUNNING
        )
        is True
    )
    loaded = store.get_job(job.id)
    assert loaded.request_id == "req-1"
    assert loaded.status == JobStatus.RUNNING
    assert store.update_job_if_unsubmitted(job.id, request_id="req-2") is False
    assert store.get_job(job.id).request_id == "req-1"


def test_try_cancel_unsubmitted(store: Store) -> None:
    _, job = _seed_job(store)
    assert store.try_cancel_unsubmitted(job.id) == "canceled"
    loaded = store.get_job(job.id)
    assert loaded.status == JobStatus.CANCELED
    assert loaded.finished_at is not None
    assert store.try_cancel_unsubmitted(job.id) == "terminal"


def test_try_cancel_submitted_returns_submitted(store: Store) -> None:
    _, job = _seed_job(store, request_id="req-9", status=JobStatus.RUNNING)
    assert store.try_cancel_unsubmitted(job.id) == "submitted"
    assert store.get_job(job.id).status == JobStatus.RUNNING


def test_update_job_blind_keeps_terminal_status(store: Store) -> None:
    _, job = _seed_job(store, status=JobStatus.CANCELED)
    store.update_job(job.id, attempts=5)
    loaded = store.get_job(job.id)
    assert loaded.attempts == 5
    assert loaded.status == JobStatus.CANCELED


def test_update_job_rejects_unknown_fields(store: Store) -> None:
    _, job = _seed_job(store)
    with pytest.raises(ValueError, match="no permitidos"):
        store.update_job(job.id, api_key="no-deberia-ir")


def test_next_version_and_jobs_for_asset(store: Store) -> None:
    _, job = _seed_job(store)
    assert store.next_version("demo", job.slug) == 2
    history = store.jobs_for_asset("demo", job.slug)
    assert len(history) == 1
    assert history[0].id == job.id


def test_active_jobs_only_non_terminal(store: Store) -> None:
    _seed_job(store)
    _seed_job(store, status=JobStatus.RUNNING, request_id="req-x")
    _seed_job(store, status=JobStatus.COMPLETED, request_id="req-y")
    active = store.active_jobs()
    assert {j.status for j in active} <= {JobStatus.PENDING, JobStatus.RUNNING}


def test_db_wal_mode(store: Store) -> None:
    _seed_job(store)
    con = sqlite3.connect(store.db_path)
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()
    assert str(mode).lower() == "wal"


def test_manifest_written_without_secrets(store: Store) -> None:
    batch, job = _seed_job(store)
    path = store.write_batch_manifest(batch, store.get_jobs(batch.id))
    text = path.read_text(encoding="utf-8")
    assert job.idempotency_key in text
    assert "HF_API_KEY" not in text
    assert "Authorization" not in text
