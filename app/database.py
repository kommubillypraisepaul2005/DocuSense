"""Small SQLite job store. Source files remain in memory and are never persisted."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_ENCRYPTION_KEY, DATABASE_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _encode(value: Any) -> str | None:
    if value is None:
        return None
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if DATA_ENCRYPTION_KEY:
        from cryptography.fernet import Fernet

        return "enc:" + Fernet(DATA_ENCRYPTION_KEY.encode("ascii")).encrypt(payload).decode("ascii")
    return "plain:" + payload.decode("utf-8")


def _decode(value: str | None) -> Any:
    if not value:
        return None
    if value.startswith("enc:"):
        from cryptography.fernet import Fernet

        payload = Fernet(DATA_ENCRYPTION_KEY.encode("ascii")).decrypt(value[4:].encode("ascii"))
        return json.loads(payload)
    return json.loads(value[6:] if value.startswith("plain:") else value)


def initialize() -> None:
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, file_type TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          result_json TEXT, error TEXT, review_status TEXT NOT NULL DEFAULT 'open', review_json TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, job_id TEXT,
          event TEXT NOT NULL, detail TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_review ON jobs(review_status, created_at);
        """)
        conn.execute("UPDATE jobs SET status='failed', error='Interrupted by service restart', updated_at=? WHERE status IN ('queued','processing')", (_now(),))


def audit(event: str, job_id: str | None = None, detail: str = "") -> None:
    with _db() as conn:
        conn.execute("INSERT INTO audit_log(at,job_id,event,detail) VALUES(?,?,?,?)", (_now(), job_id, event, detail[:500]))


def create_job(job_id: str, filename: str, file_type: str) -> dict[str, Any]:
    now = _now()
    with _db() as conn:
        conn.execute("INSERT INTO jobs(id,filename,file_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (job_id, filename, file_type, "queued", now, now))
    audit("job_queued", job_id, file_type)
    return get_job(job_id)


def set_job_status(job_id: str, status: str, error: str | None = None) -> None:
    with _db() as conn:
        conn.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, _now(), job_id))
    audit("job_" + status, job_id, error or "")


def finish_job(job_id: str, result: dict[str, Any]) -> None:
    with _db() as conn:
        conn.execute("UPDATE jobs SET status='completed',result_json=?,updated_at=? WHERE id=?", (_encode(result), _now(), job_id))
    audit("job_completed", job_id, result.get("validation", {}).get("status", ""))


def get_job(job_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT id,filename,file_type,status,created_at,updated_at,error,review_status FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_result(job_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT result_json,review_json,review_status FROM jobs WHERE id=? AND status='completed'", (job_id,)).fetchone()
    if not row or not row["result_json"]:
        return None
    result = _decode(row["result_json"])
    result["human_review"] = {"status": row["review_status"], "submission": _decode(row["review_json"])}
    return result


def list_reviews(limit: int = 100) -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT id,filename,file_type,status,created_at,updated_at,review_status,result_json FROM jobs WHERE status='completed' AND review_status='open' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        extraction = _decode(row["result_json"])
        if extraction and extraction.get("validation", {}).get("review_required"):
            result.append({"id": row["id"], "filename": row["filename"], "file_type": row["file_type"], "created_at": row["created_at"], "result": extraction})
    return result


def record_review(job_id: str, review: dict[str, Any], updated_result: dict[str, Any] | None = None) -> bool:
    with _db() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET review_status='resolved',review_json=?,result_json=COALESCE(?,result_json),updated_at=? WHERE id=? AND status='completed'",
            (_encode(review), _encode(updated_result), _now(), job_id),
        )
    if cursor.rowcount:
        audit("human_review_recorded", job_id, review.get("decision", ""))
        return True
    return False


def delete_job(job_id: str) -> bool:
    audit("job_deleted", job_id, "result and review payload removed")
    with _db() as conn:
        cursor = conn.execute("DELETE FROM jobs WHERE id=? AND status IN ('completed','failed')", (job_id,))
    return cursor.rowcount > 0


def get_metrics() -> dict[str, Any]:
    with _db() as conn:
        jobs = {row["status"]: row["count"] for row in conn.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")}
        reviews = {row["review_status"]: row["count"] for row in conn.execute("SELECT review_status,COUNT(*) AS count FROM jobs WHERE status='completed' GROUP BY review_status")}
    return {"jobs_by_status": jobs, "completed_by_review_status": reviews}
