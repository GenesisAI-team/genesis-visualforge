"""Fixtures compartidas: store temporal, proyecto demo y proveedor mock."""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from visualforge.jobs import BatchRunner, RunnerConfig, plan_batch
from visualforge.models import Batch, BatchConfig, Job, Project, PromptEntry, new_id
from visualforge.provider.mock import MockProvider
from visualforge.storage import Store

FAST_RUNNER = RunnerConfig(
    concurrency=2,
    poll_initial=0.0,
    poll_max=0.01,
    poll_jitter=0.0,
    backoff_max=0.01,
    submit_max_attempts=4,
    download_max_attempts=3,
    status_error_max=10,
)

FLARE_CONFIG = BatchConfig(
    model="marketing-studio/image/flare",
    resolution="2k",
    aspect_ratio="16:9",
    quality="high",
    concurrency=2,
)


def make_png_bytes(color: tuple[int, int, int] = (200, 120, 40), size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def wait_for(
    predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def store(projects_dir: Path) -> Store:
    return Store(projects_dir, max_reference_mb=1)


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def runner(store: Store, provider: MockProvider) -> BatchRunner:
    instance = BatchRunner(store, provider, FAST_RUNNER)
    yield instance
    instance.shutdown(wait=False)


@pytest.fixture
def project(store: Store) -> Project:
    demo = Project(
        slug="restaurante-demo",
        name="Restaurante Demo",
        brief="Web de restaurante de barrio con terraza.",
        art_direction="Paleta cálida en madera y terracota, luz natural suave.",
    )
    store.save_project(demo)
    ref_a = store.add_reference(
        demo, "terraza.png", make_png_bytes((200, 120, 40)), "image/png"
    )
    ref_b = store.add_reference(
        demo, "fachada.png", make_png_bytes((30, 90, 150)), "image/png"
    )
    demo.prompts = [
        PromptEntry(
            id=new_id(),
            name="Hero exterior",
            slug="hero-exterior",
            prompt="Gran foto de portada de la terraza al atardecer.",
            reference_ids=[ref_a.id],
        ),
        PromptEntry(
            id=new_id(),
            name="Fachada frontal",
            slug="fachada-frontal",
            prompt="Frontal del local con cartel iluminado.",
            reference_ids=[ref_b.id],
        ),
        PromptEntry(
            id=new_id(),
            name="Terraza",
            slug="terraza",
            prompt="Mesas de la terraza con ambiente cálido.",
            keep="El toldo y el cartel deben mantenerse.",
            reference_ids=[ref_a.id, ref_b.id],
            aspect_ratio="1:1",
        ),
    ]
    store.save_project(demo)
    return demo


def make_project_with_prompts(store: Store, count: int) -> Project:
    demo = Project(slug=f"proyecto-{count}", name=f"Proyecto {count} prompts")
    store.save_project(demo)
    demo.prompts = [
        PromptEntry(
            id=new_id(),
            name=f"Asset {index}",
            slug=f"asset-{index}",
            prompt=f"Prompt de prueba número {index}.",
        )
        for index in range(1, count + 1)
    ]
    store.save_project(demo)
    return demo


@pytest.fixture
def planned(
    store: Store, provider: MockProvider, project: Project
) -> tuple[Batch, list[Job]]:
    batch, jobs = plan_batch(store, provider, project, FLARE_CONFIG)
    store.create_batch(batch, jobs)
    return batch, jobs


@pytest.fixture
def completed_batch(
    store: Store,
    provider: MockProvider,
    runner: BatchRunner,
    planned: tuple[Batch, list[Job]],
) -> tuple[Batch, list[Job]]:
    batch, _ = planned
    runner.start_batch(batch.id)
    assert runner.wait(batch.id, timeout=30), "el lote no terminó a tiempo"
    return batch, store.get_jobs(batch.id)
