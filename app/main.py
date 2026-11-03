from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

from . import database
from .config import APP_API_KEY, APP_ENV, DATA_ENCRYPTION_KEY, MAX_UPLOAD_MB, OLLAMA_BASE_URL, OLLAMA_TEXT_MODEL, OLLAMA_VISION_MODEL
from .processor import process_document
from .validation import validate_extraction

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".doc", ".xls", ".ppt", ".docx", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods", ".odp", ".csv", ".txt", ".md", ".json", ".log", ".xml", ".html", ".htm", ".rtf"}
STATIC_DIR = Path(__file__).parent / "static"
RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "results"
JOB_LIMITER = asyncio.Semaphore(2)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if APP_ENV == "production" and not APP_API_KEY:
        raise RuntimeError("APP_API_KEY must be configured when APP_ENV=production")
    if APP_ENV == "production" and not DATA_ENCRYPTION_KEY:
        raise RuntimeError("DATA_ENCRYPTION_KEY must be configured when APP_ENV=production")
    if DATA_ENCRYPTION_KEY:
        from cryptography.fernet import Fernet

        Fernet(DATA_ENCRYPTION_KEY.encode("ascii"))
    database.initialize()
    yield


app = FastAPI(title="Open Document Intelligence", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if APP_API_KEY and not secrets.compare_digest(x_api_key or "", APP_API_KEY):
        raise HTTPException(401, "Valid X-API-Key header required")


class ValidationRequest(BaseModel):
    extraction: dict[str, Any] | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)


class ReviewSubmission(BaseModel):
    decision: Literal["approved", "corrected", "rejected"]
    comment: str = Field(default="", max_length=2000)
    corrected_extraction: dict[str, Any] | None = None


async def read_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file type: {suffix or '(no extension)'}")
    data = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_MB} MB limit")
    if not data:
        raise HTTPException(400, "Uploaded file is empty")
    return filename, data


def save_result_file(job_id: str, filename: str, mode: str, result: dict[str, Any]) -> dict[str, str]:
    """Persist a JSON extraction record in the user-requested data/results folder."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).strip("._-")[:70] or "document"
    result_name = f"{job_id}_{stem}.json"
    saved_at = datetime.now(timezone.utc).isoformat()
    file_info = {"filename": result_name, "path": f"data/results/{result_name}"}
    result["saved_result"] = file_info
    record = {
        "job_id": job_id,
        "source_filename": filename,
        "mode": mode,
        "saved_at": saved_at,
        "result": result,
    }
    output_path = RESULTS_DIR / result_name
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(output_path)
    return file_info


def parse_schema(schema_json: str | None) -> dict[str, Any] | None:
    if not schema_json:
        return None
    if len(schema_json) > 10000:
        raise HTTPException(413, "Extraction schema is too large")
    try:
        schema = json.loads(schema_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "schema_json must be valid JSON") from exc
    if not isinstance(schema, dict) or len(schema) > 40:
        raise HTTPException(400, "schema_json must be a JSON object with at most 40 fields")
    return schema


async def run_job(job_id: str, filename: str, data: bytes, mode: str, schema: dict[str, Any] | None) -> None:
    async with JOB_LIMITER:
        database.set_job_status(job_id, "processing")
        try:
            result = await process_document(filename, data, mode, schema)
            save_result_file(job_id, filename, mode, result)
            database.finish_job(job_id, result)
        except Exception as exc:
            database.set_job_status(job_id, "failed", f"Processing failed ({type(exc).__name__}).")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    ollama = {"reachable": False, "text_model": OLLAMA_TEXT_MODEL, "vision_model": OLLAMA_VISION_MODEL, "text_ready": False, "vision_ready": False}
    try:
        async with httpx.AsyncClient(timeout=0.8) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            installed = {str(model.get("name", "")) for model in response.json().get("models", [])}
        ollama["reachable"] = True
        ollama["text_ready"] = OLLAMA_TEXT_MODEL in installed
        ollama["vision_ready"] = OLLAMA_VISION_MODEL in installed
    except (httpx.HTTPError, ValueError):
        pass
    return {"status": "ok", "model_mode": "local Ollama optional", "ollama": ollama, "max_upload_mb": MAX_UPLOAD_MB, "api_key_enabled": bool(APP_API_KEY)}


@app.post("/extract", dependencies=[Depends(require_api_key)])
async def extract(file: UploadFile = File(...), mode: Literal["auto", "text", "vision", "vision_zero_shot", "rules"] = "auto", schema_json: str | None = Form(default=None)):
    filename, data = await read_upload(file)
    schema = parse_schema(schema_json)
    try:
        result = await process_document(filename, data, mode, schema)
        save_result_file(str(uuid.uuid4()), filename, mode, result)
        return result
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"Could not process this document: {exc}") from exc


@app.post("/documents", status_code=202, dependencies=[Depends(require_api_key)])
async def create_document(background_tasks: BackgroundTasks, file: UploadFile = File(...), mode: Literal["auto", "text", "vision", "vision_zero_shot", "rules"] = "auto", schema_json: str | None = Form(default=None)):
    filename, data = await read_upload(file)
    schema = parse_schema(schema_json)
    job_id = str(uuid.uuid4())
    database.create_job(job_id, filename, Path(filename).suffix.lower())
    background_tasks.add_task(run_job, job_id, filename, data, mode, schema)
    return {"id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}", "result_url": f"/results/{job_id}"}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(job_id: str):
    result = database.get_job(job_id)
    if result is None:
        raise HTTPException(404, "Job not found")
    return result


@app.get("/results/{job_id}", dependencies=[Depends(require_api_key)])
async def get_result(job_id: str):
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    result = database.get_result(job_id)
    if result is None:
        raise HTTPException(409, f"Result is not ready (job status: {job['status']})")
    return result


@app.get("/saved-results", dependencies=[Depends(require_api_key)])
async def list_saved_results():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(RESULTS_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:100]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            result_payload = record.get("result") if isinstance(record.get("result"), dict) else record
            items.append({
                "file": path.name,
                "source_filename": record.get("source_filename") or result_payload.get("filename") or "document",
                "saved_at": record.get("saved_at"),
                "mode": record.get("mode") or result_payload.get("mode") or result_payload.get("model_mode"),
                "size_bytes": path.stat().st_size,
            })
        except (OSError, json.JSONDecodeError):
            continue
    return {"items": items, "directory": "data/results"}


@app.get("/saved-results/{result_filename}", dependencies=[Depends(require_api_key)])
async def download_saved_result(result_filename: str):
    if Path(result_filename).name != result_filename or not result_filename.endswith(".json"):
        raise HTTPException(404, "Saved result not found")
    path = (RESULTS_DIR / result_filename).resolve()
    if path.parent != RESULTS_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Saved result not found")
    return FileResponse(path, media_type="application/json", filename=result_filename)


@app.post("/validate", dependencies=[Depends(require_api_key)])
async def validate(payload: ValidationRequest):
    return validate_extraction(payload.extraction, payload.segments)


@app.get("/reviews", dependencies=[Depends(require_api_key)])
async def list_reviews(limit: int = 50):
    return {"items": database.list_reviews(max(1, min(limit, 100)))}


@app.get("/metrics", dependencies=[Depends(require_api_key)])
async def metrics():
    return database.get_metrics()


@app.post("/reviews/{job_id}", dependencies=[Depends(require_api_key)])
async def submit_review(job_id: str, payload: ReviewSubmission):
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    existing = database.get_result(job_id)
    if existing is None:
        raise HTTPException(409, "Job is not completed and cannot be reviewed")
    if payload.decision == "corrected" and payload.corrected_extraction is None:
        raise HTTPException(400, "corrected_extraction is required when decision is corrected")

    review = payload.model_dump()
    updated = dict(existing)
    if payload.decision == "corrected":
        updated["extraction"] = payload.corrected_extraction
        updated["validation"] = validate_extraction(
            payload.corrected_extraction,
            updated.get("segments", []),
            updated.get("requested_schema"),
        )
    updated["human_review"] = {"status": "resolved", "submission": review}
    save_result_file(job_id, job["filename"], updated.get("mode", "auto"), updated)

    if not database.record_review(job_id, review, updated):
        raise HTTPException(409, "Job is not completed and cannot be reviewed")
    return {
        "id": job_id,
        "review_status": "resolved",
        "decision": payload.decision,
        "result": database.get_result(job_id),
    }


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def delete_job(job_id: str):
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job["status"] not in {"completed", "failed"} or not database.delete_job(job_id):
        raise HTTPException(409, "Only completed or failed jobs can be deleted")
    for result_path in RESULTS_DIR.glob(f"{job_id}_*.json"):
        resolved = result_path.resolve()
        if resolved.parent == RESULTS_DIR.resolve() and resolved.is_file():
            resolved.unlink()
    return {"id": job_id, "deleted": True}
