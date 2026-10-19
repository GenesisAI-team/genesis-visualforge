"""Persistencia: proyectos en disco + lotes/jobs en SQLite.

Estructura por proyecto (todo bajo ``projects/``, fuera de control de versiones)::

    projects/<slug>/
      project.json              # metadatos + referencias
      prompts.json              # prompts del proyecto
      references/               # fotografías originales del cliente
      batches/<batch-id>/
        manifest.json           # manifiesto trazable del lote
        results/<job-id>/       # originales descargados por job
      exports/
        originals/
        web/
        README-assets.md

SQLite (``projects/.visualforge.db``) guarda lotes y jobs con sus request_id,
idempotency keys, estados, errores y estimaciones. Sin secretos: la API key
jamás se persiste aquí.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import (
    ALLOWED_REFERENCE_MIME,
    Batch,
    BatchConfig,
    Job,
    JobStatus,
    Project,
    PromptEntry,
    ReferenceImage,
    ValidationError,
    new_id,
)

_BATCH_COLUMNS = {
    "id",
    "project_slug",
    "config_json",
    "status",
    "created_at",
    "finished_at",
    "regenerates_batch",
}

_JOB_COLUMNS = {
    "id",
    "batch_id",
    "project_slug",
    "prompt_id",
    "asset_name",
    "slug",
    "version",
    "model",
    "final_prompt",
    "payload_json",
    "reference_ids_json",
    "idempotency_key",
    "status",
    "request_id",
    "attempts",
    "error",
    "estimate_credits",
    "estimate_usd",
    "result_url",
    "original_path",
    "web_path",
    "regenerates",
    "created_at",
    "updated_at",
    "finished_at",
}

_JSON_JOB_FIELDS = {"payload_json", "reference_ids_json"}

_BAD_FILENAME_CHARS = re.compile(r"[^\w.\- ]+")


def sanitize_filename(filename: str) -> str:
    """Nombre de archivo local seguro conservando la extensión."""
    base = Path(filename.replace("\\", "/")).name.strip()
    base = _BAD_FILENAME_CHARS.sub("_", base).strip(" .")
    if not base:
        raise ValidationError("Nombre de archivo inválido.")
    return base[:120]


class Store:
    """Acceso a proyectos (FS) y lotes/jobs (SQLite). Hilo-seguro por conexión."""

    def __init__(self, projects_dir: Path, max_reference_mb: int = 25) -> None:
        self.projects_dir = Path(projects_dir)
        self.max_reference_mb = max_reference_mb
        self.db_path = self.projects_dir / ".visualforge.db"
        self._db_lock = threading.Lock()
        self._db_ready = False

    # ------------------------------------------------------------------
    # SQLite: conexión e inicialización
    # ------------------------------------------------------------------

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Conexión por operación: commit al finalizar y cierre garantizado."""
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            if not self._db_ready:
                with self._db_lock:
                    if not self._db_ready:
                        self._create_schema(conn)
                        self._db_ready = True
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                finished_at REAL,
                regenerates_batch TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                project_slug TEXT NOT NULL,
                prompt_id TEXT NOT NULL,
                asset_name TEXT NOT NULL,
                slug TEXT NOT NULL,
                version INTEGER NOT NULL,
                model TEXT NOT NULL,
                final_prompt TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                reference_ids_json TEXT NOT NULL DEFAULT '[]',
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL,
                request_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                estimate_credits TEXT,
                estimate_usd TEXT,
                result_url TEXT,
                original_path TEXT,
                web_path TEXT,
                regenerates TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL,
                FOREIGN KEY (batch_id) REFERENCES batches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_request ON jobs(request_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_version
                ON jobs(project_slug, slug, version);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem
                ON jobs(idempotency_key);
            """
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Proyectos (filesystem)
    # ------------------------------------------------------------------

    def project_dir(self, slug: str) -> Path:
        if not slug or "/" in slug or "\\" in slug or slug in {".", ".."}:
            raise ValidationError(f"Slug de proyecto inválido: '{slug}'")
        return self.projects_dir / slug

    def save_project(self, project: Project) -> None:
        pdir = self.project_dir(project.slug)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "project.json").write_text(
            json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (pdir / "prompts.json").write_text(
            json.dumps([p.to_dict() for p in project.prompts], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_project(self, slug: str) -> Project:
        pdir = self.project_dir(slug)
        project_file = pdir / "project.json"
        if not project_file.is_file():
            raise ValidationError(f"Proyecto no encontrado: '{slug}'")
        data = json.loads(project_file.read_text(encoding="utf-8"))
        prompts_file = pdir / "prompts.json"
        prompts: list[PromptEntry] = []
        if prompts_file.is_file():
            prompts = [
                PromptEntry.from_dict(item)
                for item in json.loads(prompts_file.read_text(encoding="utf-8"))
            ]
        return Project.from_dict(data, prompts)

    def list_projects(self) -> list[Project]:
        """Proyectos legibles; los corruptos se omiten sin tumbar la UI."""
        if not self.projects_dir.is_dir():
            return []
        projects: list[Project] = []
        for entry in sorted(self.projects_dir.iterdir()):
            if not entry.is_dir() or not (entry / "project.json").is_file():
                continue
            try:
                projects.append(self.load_project(entry.name))
            except (ValidationError, json.JSONDecodeError, OSError, KeyError):
                continue
        projects.sort(key=lambda p: p.created_at, reverse=True)
        return projects

    def add_reference(
        self,
        project: Project,
        filename: str,
        data: bytes,
        mime: str,
    ) -> ReferenceImage:
        """Valida y guarda una fotografía de referencia en el proyecto."""
        mime_norm = (mime or "").split(";")[0].strip().lower()
        if mime_norm not in ALLOWED_REFERENCE_MIME:
            raise ValidationError(
                f"Formato no soportado: '{mime or 'desconocido'}'. "
                f"Permitidos: {', '.join(sorted(ALLOWED_REFERENCE_MIME))}."
            )
        if not data:
            raise ValidationError("El archivo está vacío.")
        max_bytes = self.max_reference_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise ValidationError(
                f"El archivo supera el límite local de {self.max_reference_mb} MB."
            )

        safe_name = sanitize_filename(filename)
        ref_id = new_id()
        refs_dir = self.project_dir(project.slug) / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{ref_id[:8]}-{safe_name}"
        (refs_dir / stored_name).write_bytes(data)

        reference = ReferenceImage(
            id=ref_id,
            filename=safe_name,
            path=f"references/{stored_name}",
            mime=mime_norm,
            size_bytes=len(data),
        )
        project.references.append(reference)
        self.save_project(project)
        return reference

    def reference_path(self, project: Project, reference_path: str) -> Path:
        return self.project_dir(project.slug) / reference_path

    # ------------------------------------------------------------------
    # Lotes y jobs (SQLite)
    # ------------------------------------------------------------------

    @staticmethod
    def _job_to_row(job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "batch_id": job.batch_id,
            "project_slug": job.project_slug,
            "prompt_id": job.prompt_id,
            "asset_name": job.asset_name,
            "slug": job.slug,
            "version": job.version,
            "model": job.model,
            "final_prompt": job.final_prompt,
            "payload_json": json.dumps(job.payload, ensure_ascii=False),
            "reference_ids_json": json.dumps(job.reference_ids),
            "idempotency_key": job.idempotency_key,
            "status": str(job.status),
            "request_id": job.request_id,
            "attempts": job.attempts,
            "error": job.error,
            "estimate_credits": job.estimate_credits,
            "estimate_usd": job.estimate_usd,
            "result_url": job.result_url,
            "original_path": job.original_path,
            "web_path": job.web_path,
            "regenerates": job.regenerates,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
        }

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        data = dict(row)
        payload = json.loads(data.pop("payload_json"))
        reference_ids = json.loads(data.pop("reference_ids_json"))
        data["payload"] = payload
        data["reference_ids"] = reference_ids
        data["status"] = JobStatus(data["status"])
        return Job(**data)

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> Batch:
        data = dict(row)
        config = BatchConfig.from_dict(json.loads(data.pop("config_json")))
        return Batch(config=config, **data)

    def create_batch(self, batch: Batch, jobs: list[Job]) -> None:
        columns = sorted(_JOB_COLUMNS)
        placeholders = ", ".join("?" for _ in columns)
        column_names = ", ".join(columns)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO batches (id, project_slug, config_json, status,"
                " created_at, finished_at, regenerates_batch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch.id,
                    batch.project_slug,
                    json.dumps(batch.config.to_dict()),
                    batch.status,
                    batch.created_at,
                    batch.finished_at,
                    batch.regenerates_batch,
                ),
            )
            for job in jobs:
                row = self._job_to_row(job)
                conn.execute(
                    f"INSERT INTO jobs ({column_names}) VALUES ({placeholders})",
                    tuple(row[col] for col in columns),
                )

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return self._batch_from_row(row) if row else None

    def list_batches(self, project_slug: str) -> list[Batch]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM batches WHERE project_slug = ? ORDER BY created_at DESC",
                (project_slug,),
            ).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def get_jobs(self, batch_id: str) -> list[Job]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at ASC, version ASC",
                (batch_id,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def get_job(self, job_id: str) -> Job | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row else None

    def get_job_by_request_id(self, request_id: str) -> Job | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._job_from_row(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        """Actualiza campos del job (whitelist) y refresca ``updated_at``."""
        unknown = set(fields) - _JOB_COLUMNS
        if unknown:
            raise ValueError(f"Campos no permitidos: {sorted(unknown)}")
        if not fields:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key == "status":
                value = str(JobStatus(value))
            elif key in _JSON_JOB_FIELDS and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(time.time())
        values.append(job_id)
        with self._connection() as conn:
            conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                tuple(values),
            )

    def set_batch_status(
        self, batch_id: str, status: str, finished_at: float | None = None
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE batches SET status = ?, finished_at = COALESCE(?, finished_at)"
                " WHERE id = ?",
                (status, finished_at, batch_id),
            )

    def update_job_if_unsubmitted(self, job_id: str, **fields: Any) -> bool:
        """Actualiza el job SOLO si aún no tiene request_id (sin carreras).

        Devuelve False si el job fue cancelado (u otro hilo ya registró su
        request_id) mientras se enviaba: en ese caso el llamante debe decidir
        cómo tratar la generación ya aceptada por el proveedor.
        """
        unknown = set(fields) - _JOB_COLUMNS
        if unknown:
            raise ValueError(f"Campos no permitidos: {sorted(unknown)}")
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key == "status":
                value = str(JobStatus(value))
            elif key in _JSON_JOB_FIELDS and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(time.time())
        values.append(job_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)}"
                " WHERE id = ? AND request_id IS NULL"
                " AND status IN ('pending', 'queued')",
                tuple(values),
            )
            return cursor.rowcount > 0

    def try_cancel_unsubmitted(self, job_id: str) -> str:
        """Cancela un job solo si aún no se ha enviado.

        Devuelve ``"canceled"`` (cancelado aquí, sin cargo),
        ``"submitted"`` (ya tiene request_id en el proveedor) o
        ``"terminal"`` (ya finalizado o inexistente).
        """
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status, request_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] in {
                str(JobStatus.COMPLETED),
                str(JobStatus.FAILED),
                str(JobStatus.CANCELED),
            }:
                return "terminal"
            if row["request_id"]:
                return "submitted"
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ?, updated_at = ?"
                " WHERE id = ? AND request_id IS NULL AND status IN ('pending','queued')",
                (
                    str(JobStatus.CANCELED),
                    "Cancelado antes de enviarse (sin cargo).",
                    now,
                    now,
                    job_id,
                ),
            )
            if cursor.rowcount > 0:
                return "canceled"
        # Alguien lo envió entre la lectura y la actualización.
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status, request_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None or row["status"] == str(JobStatus.CANCELED):
            return "terminal"
        return "submitted"

    def next_version(self, project_slug: str, slug: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM jobs"
                " WHERE project_slug = ? AND slug = ?",
                (project_slug, slug),
            ).fetchone()
        return int(row["max_version"]) + 1

    def jobs_for_asset(self, project_slug: str, slug: str) -> list[Job]:
        """Historial de versiones de un asset (más reciente primero)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_slug = ? AND slug = ?"
                " ORDER BY version DESC",
                (project_slug, slug),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def active_jobs(self) -> list[Job]:
        """Jobs no terminados (para reanudar tras reiniciar la app)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('pending', 'queued', 'running')"
                " ORDER BY created_at ASC"
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def list_project_jobs(self, project_slug: str) -> list[Job]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_slug = ? ORDER BY created_at DESC",
                (project_slug,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Manifiesto de lote
    # ------------------------------------------------------------------

    def write_batch_manifest(self, batch: Batch, jobs: list[Job]) -> Path:
        """Manifiesto trazable sin secretos (ni API key ni URLs firmadas de subida)."""
        bdir = self.project_dir(batch.project_slug) / "batches" / batch.id
        bdir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "batch": batch.to_dict(),
            "jobs": [
                {
                    "id": job.id,
                    "asset_name": job.asset_name,
                    "slug": job.slug,
                    "version": job.version,
                    "model": job.model,
                    "status": str(job.status),
                    "final_prompt": job.final_prompt,
                    "payload": job.payload,
                    "reference_ids": job.reference_ids,
                    "idempotency_key": job.idempotency_key,
                    "request_id": job.request_id,
                    "attempts": job.attempts,
                    "error": job.error,
                    "estimate_credits": job.estimate_credits,
                    "estimate_usd": job.estimate_usd,
                    "original_path": job.original_path,
                    "web_path": job.web_path,
                    "regenerates": job.regenerates,
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                }
                for job in jobs
            ],
        }
        path = bdir / "manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def batch_results_dir(self, batch_id: str, project_slug: str) -> Path:
        path = self.project_dir(project_slug) / "batches" / batch_id / "results"
        path.mkdir(parents=True, exist_ok=True)
        return path
