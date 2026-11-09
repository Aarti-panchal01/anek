"""Anek HTTP API and static frontend host."""

import asyncio
from array import array
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from difflib import SequenceMatcher

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
try:
    from rapidfuzz.fuzz import ratio
except ImportError:
    ratio = lambda left, right: SequenceMatcher(None, left, right).ratio() * 100

from backend import db
from backend.atomic_files import (
    StorageFullError, atomic_write_bytes, atomic_write_text, commit_temporary,
    is_storage_full, mark_invalid_media_for_regeneration, run_atomic_command,
    temporary_path,
)
from backend.brain.checks import run_checks
from backend.clients.gnani import create_gnani_client
from backend.brain.termsheet import TermSheet
from backend.config import LANGUAGES, get_settings
from backend.pipeline.phase3b import (
    MAX_ATEMPO_SPEEDUP, OVERRUN_TOLERANCE_S, prepare_language_retry_checkpoint, run_quality_pass,
)
from backend.pipeline.timing import schedule_language
from backend.translation_style import eligible_style_switch_rows, style_switch_eligible

settings = get_settings()
LOGGER = logging.getLogger(__name__)
_shutdown_requested = threading.Event()
_active_job_tasks: set[asyncio.Task] = set()
_preview_tasks: dict[str, asyncio.Task] = {}
_project_language_locks: dict[tuple[str, str], asyncio.Lock] = {}


class JobInterrupted(Exception):
    pass


async def schedule_job(job_id: str) -> None:
    if _shutdown_requested.is_set():
        raise JobInterrupted("Service is shutting down; job remains resumable")
    task = asyncio.create_task(run_job(job_id))
    _active_job_tasks.add(task)
    task.add_done_callback(_active_job_tasks.discard)


async def prepare_project_preview(project_id: str) -> None:
    """Prepare one browser copy per upload and persist its outcome."""
    current = asyncio.current_task()
    try:
        project = project_row(project_id)
        original_value = project.get("original_video_path") or project.get("video_path")
        if not original_value:
            raise FileNotFoundError("Original upload is missing")
        original = Path(original_value)
        if not original.is_file():
            raise FileNotFoundError("Original upload is missing")
        reason = await asyncio.to_thread(_browser_transcode_reason, original)
        if reason is None:
            playable = original
        else:
            playable = original.parent / "source-browser.mp4"
            await asyncio.to_thread(create_browser_compatible_copy, original, playable)
        with db.connect() as connection:
            connection.execute(
                "UPDATE projects SET video_path=?,preview_path=?,preview_status='ready',preview_error=NULL WHERE id=?",
                (str(playable), str(playable), project_id),
            )
    except Exception as error:
        LOGGER.exception("Project browser preview preparation failed project_id=%s", project_id)
        try:
            project = project_row(project_id)
            original = project.get("original_video_path") or project.get("video_path")
            with db.connect() as connection:
                connection.execute(
                    "UPDATE projects SET video_path=?,preview_path=NULL,preview_status='failed',preview_error=? WHERE id=?",
                    (original, str(error)[:1000], project_id),
                )
        except Exception:
            LOGGER.exception("Could not persist preview failure project_id=%s", project_id)
    finally:
        if _preview_tasks.get(project_id) is current:
            _preview_tasks.pop(project_id, None)


def queue_project_preview(project_id: str) -> asyncio.Task | None:
    existing = _preview_tasks.get(project_id)
    if existing and not existing.done():
        return existing
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(prepare_project_preview(project_id))
    _preview_tasks[project_id] = task
    return task


def log_runtime_configuration(config=settings, environment: dict[str, str] | None = None) -> None:
    env = os.environ if environment is None else environment
    LOGGER.info(
        "Effective timing config FIT_TOLERANCE=%.3f MAX_STRETCH=%.3f SILENT_GAP_SPILL=enabled "
        "OVERRUN_TOLERANCE_S=%.3f SPEEDUP_CAP=%.2fx",
        config.fit_tolerance, config.max_stretch, OVERRUN_TOLERANCE_S, MAX_ATEMPO_SPEEDUP,
    )
    providers = []
    if config.gnani_mock:
        providers.append("Gnani (mock)")
    elif config.gnani_api_key:
        providers.append("Gnani")
    if config.llm_mock:
        providers.append("Sarvam (mock)")
    elif config.sarvam_api_key:
        providers.append("Sarvam")
    LOGGER.info("Enabled providers: %s", ", ".join(providers) if providers else "none")
    overrides = [name for name in ("FIT_TOLERANCE", "MAX_STRETCH") if name in env]
    railway = any(name in env for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))
    if railway:
        message = f"Railway timing environment overrides code defaults: {', '.join(overrides)}" if overrides else "Railway timing environment overrides code defaults: none"
        (LOGGER.warning if overrides else LOGGER.info)(message)
    elif overrides:
        LOGGER.info("Timing environment overrides code defaults: %s", ", ".join(overrides))
app = FastAPI(title=settings.app_name)


def _require_privacy_delete_password(request: Request) -> None:
    expected = os.getenv("PRIVACY_DELETE_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "Deletion password is not configured on the server.")
    supplied = request.headers.get("x-privacy-delete-password", "")
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(403, "Deletion password is incorrect.")
DATA_ROOT = Path(settings.anek_data_dir)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
UPLOAD_WORKING_COPY_MULTIPLIER = 2
STORAGE_BUDGET_BYTES = 350 * 1024 * 1024
CONSENT_PHRASES = [
    "Today I am recording my voice for an AI dubbing test.",
    "I consent to use this recording to create my own voice model.",
    "This is my voice, and I am choosing to create an AI dub.",
]
consent_sessions: dict[str, str] = {}
_rate_events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_login_events: dict[str, deque[float]] = defaultdict(deque)


def _issue_access_token() -> str:
    expires = str(int(time.time()) + 7 * 24 * 60 * 60).encode("ascii")
    signature = hmac.new(settings.anek_auth_secret.encode(), expires, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(expires).decode().rstrip("=") + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _valid_access_token(token: str) -> bool:
    try:
        encoded_expiry, encoded_signature = token.split(".", 1)
        expiry_bytes = base64.urlsafe_b64decode(encoded_expiry + "=" * (-len(encoded_expiry) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(settings.anek_auth_secret.encode(), expiry_bytes, hashlib.sha256).digest()
        return int(expiry_bytes) > int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    if not settings.require_access_auth or not request.url.path.startswith("/api/") or request.url.path == "/api/auth/login" or request.method == "OPTIONS":
        return await call_next(request)
    if not settings.anek_access_password or not settings.anek_auth_secret:
        return JSONResponse({"detail": "Application access is not configured."}, status_code=503)
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else request.query_params.get("token", "")
    if not _valid_access_token(token):
        return JSONResponse({"detail": "Sign in to use Anek."}, status_code=401)
    return await call_next(request)


origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
if origins or settings.cors_origin_regex:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Privacy-Delete-Password"],
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
    """Keep API errors structured and let CORS middleware decorate the response."""
    storage_full = is_storage_full(error)
    if storage_full:
        reason = getattr(error, "message", "Server storage is full")
        LOGGER.error("Storage full while handling %s %s", request.method, request.url.path)
        match = re.search(r"/api/projects/([A-Za-z0-9_-]+)", request.url.path)
        if match:
            try:
                with db.connect() as connection:
                    connection.execute(
                        "UPDATE jobs SET state='failed',stage='failed',error=?,updated_at=CURRENT_TIMESTAMP "
                        "WHERE project_id=? AND state='running'", (reason, match.group(1)),
                    )
                    connection.execute("UPDATE projects SET status='failed' WHERE id=?", (match.group(1),))
            except Exception:
                LOGGER.exception("Could not mark project failed after storage exhaustion")
        return JSONResponse(
            {"error": "storage_full", "message": reason}, status_code=507,
            headers=_error_cors_headers(request),
        )
    LOGGER.exception("Unhandled API failure for %s %s", request.method, request.url.path)
    return JSONResponse(
        {"error": "internal_error", "message": "The server could not complete this request."},
        status_code=500, headers=_error_cors_headers(request),
    )


def _error_cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    if not origin:
        return {}
    allowed = origin in origins or "*" in origins
    if not allowed and settings.cors_origin_regex:
        try:
            allowed = re.fullmatch(settings.cors_origin_regex, origin) is not None
        except re.error:
            allowed = False
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if allowed else {}


def preflight_upload_space(size_bytes: int) -> None:
    if size_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(DATA_ROOT).free
    required_bytes = size_bytes * UPLOAD_WORKING_COPY_MULTIPLIER
    if free_bytes < required_bytes:
        raise StorageFullError()


def _browser_transcode_reason(source: Path) -> str | None:
    """Return why a source needs a browser-compatible video transcode."""
    metadata = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=format_name:stream=codec_type,codec_name,pix_fmt:stream_tags=rotate:stream_side_data=rotation",
         "-of", "json", str(source)], check=True, capture_output=True, text=True,
    )
    payload = json.loads(metadata.stdout)
    streams = payload.get("streams", [])
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    if not video:
        return "missing video stream"
    if source.suffix.lower() != ".mp4" or "mp4" not in (payload.get("format", {}).get("format_name") or "").split(","):
        return "container is not browser-compatible MP4"
    if video.get("codec_name") != "h264":
        return f"video codec {video.get('codec_name') or 'unknown'} is not H.264"
    pixel_format = video.get("pix_fmt") or ""
    if pixel_format != "yuv420p":
        return f"pixel format {pixel_format or 'unknown'} is not 8-bit 4:2:0"
    rotation = video.get("tags", {}).get("rotate")
    if rotation is None:
        rotation = next((side.get("rotation") for side in video.get("side_data_list", [])
                         if side.get("rotation") is not None), 0)
    try:
        rotated = float(rotation) % 360 != 0
    except (TypeError, ValueError):
        rotated = True
    if rotated:
        return f"video has rotation metadata ({rotation})"
    return None


def create_browser_compatible_copy(source: Path, output: Path) -> None:
    """Transcode only incompatible video, without scaling, in a single low-memory pass."""
    run_atomic_command(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
         "-threads", "1", "-threads:v", "1", "-c:v", "libx264", "-preset", "veryfast",
         "-vf", "scale=in_range=full:out_range=tv,format=yuv420p", "-color_range", "tv",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(output)],
        check=True, capture_output=True, timeout=240,
    )
    _validate_media_file(output)
    reason = _browser_transcode_reason(output)
    if reason:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"Transcoded video did not pass browser compatibility checks: {reason}")


def _delete_project_data(project_id: str) -> dict:
    project = project_row(project_id)
    data_root = DATA_ROOT.resolve()
    for folder in (DATA_ROOT / "uploads" / project_id, DATA_ROOT / "exports" / project_id):
        resolved = folder.resolve()
        if resolved.is_relative_to(data_root) and resolved != data_root:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            if resolved.exists():
                raise HTTPException(500, "Could not remove all project files")
    with db.connect() as connection:
        connection.execute("DELETE FROM dubs WHERE clip_id IN (SELECT id FROM clips WHERE project_id=?)", (project_id,))
        connection.execute("DELETE FROM clips WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM terms WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM export_events WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM credit_ledger WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"id": project_id, "name": project["name"]}


def managed_data_bytes() -> int:
    total = 0
    if not DATA_ROOT.exists():
        return 0
    root = DATA_ROOT.resolve()
    for path in DATA_ROOT.rglob("*"):
        try:
            if path.is_file() and path.resolve().is_relative_to(root):
                total += path.stat().st_size
        except OSError:
            LOGGER.warning("Could not measure stored file path=%s", path)
    return total


def ensure_storage_room(project_id: str, upload_size: int) -> list[dict[str, str]]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    required = max(0, upload_size) * UPLOAD_WORKING_COPY_MULTIPLIER
    disk = shutil.disk_usage(DATA_ROOT)
    budget = min(disk.total, STORAGE_BUDGET_BYTES)
    if managed_data_bytes() + required > budget or disk.free < required:
        raise StorageFullError("Not enough storage for this video. Delete a project only if you choose to free space.")
    return []


def cleanup_orphaned_files(root: Path) -> list[Path]:
    removed = []
    database_path = db.DB_PATH.resolve()
    if not root.exists():
        return removed
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            if resolved == database_path or path.relative_to(root).parts[0] == "voices":
                continue
            if path.name.endswith(".tmp") or path.stat().st_size == 0:
                path.unlink(missing_ok=True)
                removed.append(path)
        except OSError:
            LOGGER.warning("Could not clean incomplete storage file path=%s", path)
    return removed


def mark_missing_project_sources() -> list[str]:
    missing = []
    with db.connect() as connection:
        rows = connection.execute("SELECT id,video_path FROM projects WHERE video_path IS NOT NULL AND status!='files_removed'").fetchall()
        for row in rows:
            if not Path(row["video_path"]).is_file():
                connection.execute("UPDATE projects SET status='files_removed' WHERE id=?", (row["id"],))
                missing.append(row["id"])
    return missing


def cleanup_project_intermediates(project_id: str) -> list[Path]:
    with db.connect() as connection:
        resumable = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id=? AND state IN ('pending','running','interrupted')",
            (project_id,),
        ).fetchone()[0]
        if resumable:
            return []
    removed = []
    export_dir = DATA_ROOT / "exports" / project_id
    clip_audio = export_dir / "clips"
    if clip_audio.is_dir():
        removed.extend(path for path in clip_audio.rglob("*") if path.is_file())
        shutil.rmtree(clip_audio, ignore_errors=True)
    work_dir = export_dir / ".anek-tmp"
    if work_dir.is_dir():
        removed.extend(path for path in work_dir.rglob("*") if path.is_file())
        shutil.rmtree(work_dir, ignore_errors=True)
    if export_dir.is_dir():
        for path in export_dir.glob("track-*.wav"):
            path.unlink(missing_ok=True)
            removed.append(path)
    preview = DATA_ROOT / "uploads" / project_id / "browser.mp4"
    if preview.is_file():
        preview.unlink(missing_ok=True)
        removed.append(preview)
    if clip_audio.exists() or work_dir.exists():
        LOGGER.warning("Some project intermediates could not be removed for %s", project_id)
    if removed:
        with db.connect() as connection:
            connection.execute(
                "UPDATE dubs SET audio_path=NULL WHERE clip_id IN (SELECT id FROM clips WHERE project_id=?)",
                (project_id,),
            )
    return removed


def enforce_ip_limit(request: Request, category: str) -> None:
    limits = {}
    for entry in settings.per_ip_limits.split(","):
        try:
            name, count = entry.strip().split("=", 1)
            limits[name.strip()] = int(count)
        except (ValueError, TypeError):
            continue
    limit = limits.get(category)
    if limit is None or limit < 1:
        return
    ip = request.client.host if request.client else "unknown"
    key = (ip, category)
    now = time.monotonic()
    events = _rate_events[key]
    while events and now - events[0] >= 3600:
        events.popleft()
    if len(events) >= limit:
        retry_after = max(1, int(3600 - (now - events[0])))
        raise HTTPException(429, f"Hourly {category} limit reached", headers={"Retry-After": str(retry_after)})
    events.append(now)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_lang: str = "auto"
    target_langs: list[str]
    translation_style: str = "natural"


class ProjectRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TranslationStyleChange(BaseModel):
    style: str
    confirm: bool = False


class AccessLogin(BaseModel):
    password: str


class TermsUpdate(BaseModel):
    terms: list[dict]
    idioms: list[dict] = []


class TermApply(BaseModel):
    term_id: str
    replacement: str
    preview: bool = True


class DubEdit(BaseModel):
    translation: str


class VerifyDecision(BaseModel):
    confirm_override: bool = False
    note: str | None = None


class BalanceRequest(BaseModel):
    lang: str
    timing_offsets: dict[str, float] = Field(default_factory=dict)
    max_shift_s: float = Field(default=1.5, ge=0, le=3)
    max_speedup: float = Field(default=1.15, ge=1, le=1.25)
    pack_all: bool = False
    apply: bool = True


class TrimRequest(BaseModel):
    tighten_pauses: bool = False


class TimelinePlacement(BaseModel):
    clip_id: str
    start_s: float = Field(ge=0)
    speed_factor: float = Field(default=1.0, ge=1.0, le=1.25)
    trim_head_s: float = Field(default=0.0, ge=0)
    trim_tail_s: float = Field(default=0.0, ge=0)
    pause_tightening: bool = False
    manual: bool = True


class TimelinePlacementRequest(BaseModel):
    placements: list[TimelinePlacement]


class SourceEdit(BaseModel):
    source_text: str = Field(min_length=1, max_length=5000)


class StartJob(BaseModel):
    voice_id: str | None = None
    voice_mode: str = "clone"
    stock_voice: str | None = None
    content_type: str = "explainer"
    translation_style: str = "natural"


class StockVoicePreview(BaseModel):
    voice: str
    language: str


STOCK_VOICES = {
    "Avinash": "Professional, steady delivery",
    "Shivani": "Warm, empathetic delivery",
    "Lakshmi": "Expressive, energetic delivery",
}
STOCK_PREVIEW_LINES = {
    "bn-IN": "নমস্কার! এটি আনেকের কণ্ঠের একটি ছোট নমুনা।",
    "en-IN": "Hello! This is a short voice sample from Anek.",
    "gu-IN": "નમસ્તે! આ એનેકના અવાજનો નાનો નમૂનો છે.",
    "hi-IN": "नमस्ते! यह Anek की आवाज़ का एक छोटा सा नमूना है।",
    "kn-IN": "ನಮಸ್ಕಾರ! ಇದು ಅನೆಕ್ ಧ್ವನಿಯ ಒಂದು ಚಿಕ್ಕ ಮಾದರಿ.",
    "ml-IN": "നമസ്കാരം! ഇത് അനെക്കിന്റെ ശബ്ദത്തിന്റെ ഒരു ചെറിയ മാതൃകയാണ്.",
    "mr-IN": "नमस्कार! हा Anek आवाजाचा एक छोटासा नमुना आहे.",
    "pa-IN": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਇਹ Anek ਦੀ ਆਵਾਜ਼ ਦਾ ਇੱਕ ਛੋਟਾ ਨਮੂਨਾ ਹੈ।",
    "ta-IN": "வணக்கம்! இது Anek குரலின் ஒரு சிறிய மாதிரி.",
    "te-IN": "నమస్కారం! ఇది Anek స్వరానికి ఒక చిన్న నమూనా.",
}


class ExportRequest(BaseModel):
    lang: str
    formats: list[str] = Field(default_factory=lambda: ["mp4", "srt", "report"], min_length=1)
    allow_unresolved_red: bool = False


class GlobalTermRequest(BaseModel):
    term: str = Field(min_length=1, max_length=240)
    type: str
    treatment: str
    targets: dict[str, str] = Field(default_factory=dict)
    locked: bool = False
    note: str = ""
    rule_type: str = "standard"
    heard_variants: list[str] = Field(default_factory=list)
    spoken_forms: dict[str, str] = Field(default_factory=dict)


def new_id() -> str:
    return uuid.uuid4().hex


def term_row_payload(row) -> dict:
    item = dict(row)
    item["locked"] = bool(item.get("locked"))
    item["targets"] = json.loads(item.pop("targets_json", "{}") or "{}")
    item["heard_variants"] = json.loads(item.pop("heard_variants_json", "[]") or "[]")
    item["spoken_forms"] = json.loads(item.pop("spoken_forms_json", "{}") or "{}")
    return item


def persist_generated_dub(connection, clip_id: str, lang: str, clip: dict) -> None:
    dub_id = new_id()
    take_id = new_id()
    connection.execute(
        "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,auto_status,reasons_json,attempts_json,audio_path,duration_s,audio_stale,user_verified,verified_fingerprint,verified_at,verify_override,verify_override_note,selected_take_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL,0,NULL,?) "
        "ON CONFLICT(clip_id,lang) DO UPDATE SET translation=CASE WHEN dubs.human_edited=1 THEN dubs.translation ELSE excluded.translation END,draft_translation=CASE WHEN dubs.human_edited=1 THEN dubs.draft_translation ELSE excluded.draft_translation END,status=excluded.status,auto_status=excluded.status,reasons_json=excluded.reasons_json,attempts_json=excluded.attempts_json,audio_path=excluded.audio_path,duration_s=excluded.duration_s,audio_stale=0,user_verified=0,verified_fingerprint=NULL,verified_at=NULL,verify_override=0,verify_override_note=NULL,selected_take_id=excluded.selected_take_id,updated_at=CURRENT_TIMESTAMP",
        (dub_id, clip_id, lang, clip["translation"], clip["translation"], clip["status"], clip["status"],
         json.dumps(clip["reasons"], ensure_ascii=False), json.dumps(clip.get("translation_synthesis_attempts", [])),
         str(clip["audio_path"]), clip.get("final_audio_duration_s"), take_id),
    )


def project_row(project_id: str):
    with db.connect() as connection:
        result = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if result is None:
        raise HTTPException(404, "Project not found")
    return dict(result)


def verification_fingerprint(row: dict | object) -> str:
    """Bind approval only to its text, selected take, and coarse timeline placement."""
    offset = float(row["timing_offset_s"] or 0)
    keys = set(row.keys())
    take_id = (row["selected_take_id"] if "selected_take_id" in keys else None) or (row["dub_id"] if "dub_id" in keys else row["id"])
    value = {
        "translation": row["translation"],
        "take_id": take_id,
        "start_s": round(float(row["start_s"]) + offset, 1),
        "end_s": round(float(row["end_s"]) + offset, 1),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def verification_audio_path(project_id: str, lang: str, clip_audio_path: str | Path | None) -> Path | None:
    """Use the clip take when present, otherwise the current mixed dub track."""
    export_root = (DATA_ROOT / "exports" / project_id).resolve()
    candidates = [Path(clip_audio_path).resolve()] if clip_audio_path else []
    mixed_track = _track_artifact_path(project_id, lang, "dubbed")
    if mixed_track:
        candidates.append(mixed_track.resolve())
    dubbed_video = _language_artifacts(project_id, lang).get("mp4")
    if dubbed_video:
        candidates.append(dubbed_video.resolve())
    for path in candidates:
        try:
            if path.is_relative_to(export_root) and path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def safe_upload_name(filename: str | None) -> str:
    return Path(filename or "upload.bin").name


def probe_video(path: Path) -> tuple[float, bool]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(probe.stdout.strip()), bool(streams.stdout.strip())


def browser_video_path(source: Path) -> Path:
    """Return the source when playable; otherwise make one browser-compatible copy."""
    browser_copy = source.parent / "browser.mp4"
    if browser_copy.is_file() and browser_copy.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return browser_copy
    try:
        reason = _browser_transcode_reason(source)
    except Exception as error:
        LOGGER.warning("Could not inspect upload for browser playback path=%s error=%s; serving original", source, error)
        return source
    if reason is None:
        return source
    try:
        LOGGER.info("Creating browser-compatible source copy path=%s reason=%s", source, reason)
        create_browser_compatible_copy(source, browser_copy)
    except Exception:
        LOGGER.exception("Browser preview conversion failed path=%s; serving original upload", source)
        return source
    return browser_copy


def mp4_has_faststart(path: Path) -> bool:
    """Check that the top-level moov atom precedes mdat for progressive playback."""
    moov_position = None
    mdat_position = None
    try:
        file_size = path.stat().st_size
        with path.open("rb") as video:
            position = 0
            while position + 8 <= file_size:
                video.seek(position)
                atom_size = int.from_bytes(video.read(4), "big")
                atom_type = video.read(4)
                header_size = 8
                if atom_size == 1:
                    atom_size = int.from_bytes(video.read(8), "big")
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - position
                if atom_size < header_size or position + atom_size > file_size:
                    return False
                if atom_type == b"moov":
                    moov_position = position
                elif atom_type == b"mdat":
                    mdat_position = position
                position += atom_size
                if moov_position is not None and mdat_position is not None:
                    return moov_position < mdat_position
        return False
    except OSError:
        return False


def browser_dubbed_video_path(source: Path) -> Path:
    """Return playable dubbed media, remuxing or transcoding only when required."""
    browser_copy = source.with_name(f"{source.stem}.browser.mp4")
    if browser_copy.is_file() and browser_copy.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return browser_copy
    reason = _browser_transcode_reason(source)
    if reason is None and mp4_has_faststart(source):
        return source
    if reason is None:
        run_atomic_command(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
             "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", str(browser_copy)],
            check=True, capture_output=True, timeout=180,
        )
    else:
        create_browser_compatible_copy(source, browser_copy)
    return browser_copy


def media_response(path: Path, request: Request) -> Response:
    """Serve a media file with single byte-range support for browser seeking."""
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return Response(status_code=416, headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        length = int(last)
        start, end = max(0, size - length), size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)

    def chunks():
        with path.open("rb") as media:
            media.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = media.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(chunks(), status_code=206, media_type=media_type, headers={
        "Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    })


def estimated_job_credits(duration_s: float, language_count: int) -> int:
    return max(1, math.ceil(duration_s / 12) * language_count * 4)


@app.on_event("startup")
async def startup() -> None:
    _shutdown_requested.clear()
    db.initialize()
    log_runtime_configuration()
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(DATA_ROOT)
    free_pct = disk.free / disk.total if disk.total else 0
    managed_used = managed_data_bytes()
    LOGGER.info("Storage at startup volume_total_bytes=%s volume_free_bytes=%s managed_used_bytes=%s free_percent=%.1f",
                disk.total, disk.free, managed_used, free_pct * 100)
    budget = min(disk.total, STORAGE_BUDGET_BYTES)
    if managed_used >= budget * 0.8 or free_pct < 0.2:
        LOGGER.warning("Storage is nearing its limit managed_used_bytes=%s budget_bytes=%s volume_free_bytes=%s", managed_used, budget, disk.free)
    mark_invalid_media_for_regeneration(DATA_ROOT / "exports", LOGGER)
    removed = cleanup_orphaned_files(DATA_ROOT)
    if removed:
        LOGGER.warning("Removed %s orphaned temporary or zero-byte files at startup", len(removed))
    missing_projects = mark_missing_project_sources()
    if missing_projects:
        LOGGER.warning("Marked %s projects as files_removed because their source files are missing", len(missing_projects))
    with db.connect() as connection:
        interrupted = connection.execute("SELECT DISTINCT project_id FROM jobs WHERE state='running'").fetchall()
        connection.execute("UPDATE jobs SET state='interrupted', stage='interrupted', error='Worker stopped before completion; resume to continue.' WHERE state='running'")
        for row in interrupted:
            connection.execute("UPDATE projects SET status='interrupted' WHERE id=?", (row["project_id"],))
        pending = connection.execute("SELECT id FROM jobs WHERE state='pending'").fetchall()
        preview_pending = connection.execute("SELECT id FROM projects WHERE preview_status='pending' AND original_video_path IS NOT NULL").fetchall()
    for item in preview_pending:
        queue_project_preview(item["id"])
    for item in pending:
        await schedule_job(item["id"])


@app.on_event("shutdown")
async def shutdown() -> None:
    _shutdown_requested.set()
    if _active_job_tasks:
        await asyncio.wait(tuple(_active_job_tasks), timeout=25)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "auth_required": settings.require_access_auth}


@app.get("/api/storage")
async def storage_status() -> dict:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(DATA_ROOT)
    budget = min(usage.total, STORAGE_BUDGET_BYTES)
    managed_used = managed_data_bytes()
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "managed_used_bytes": managed_used,
        "free_bytes": usage.free,
        "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        "free_percent": round(usage.free / usage.total * 100, 1) if usage.total else 0,
        "budget_bytes": budget,
        "budget_percent": round(budget / usage.total * 100, 1) if usage.total else 0,
        "budget_used_percent": round(managed_used / budget * 100, 1) if budget else 0,
    }


@app.post("/api/auth/login")
async def access_login(body: AccessLogin, request: Request) -> dict:
    if not settings.require_access_auth:
        raise HTTPException(404, "Sign-in is not enabled")
    if not settings.anek_access_password or not settings.anek_auth_secret:
        raise HTTPException(503, "Application access is not configured")
    address = request.client.host if request.client else "unknown"
    events = _login_events[address]
    now = time.monotonic()
    while events and now - events[0] >= 3600:
        events.popleft()
    if len(events) >= 8:
        raise HTTPException(429, "Too many sign-in attempts. Try again later.", headers={"Retry-After": "3600"})
    if not secrets.compare_digest(body.password, settings.anek_access_password):
        events.append(now)
        raise HTTPException(401, "That access password is incorrect")
    events.clear()
    return {"access_token": _issue_access_token(), "token_type": "bearer", "expires_in": 604800}


@app.get("/api/config")
async def get_config() -> dict:
    with db.connect() as connection:
        used = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
    return {
        "app_name": settings.app_name,
        "languages": {code: LANGUAGES[code] for code in settings.enabled_langs if code in LANGUAGES},
        "max_video_seconds": settings.max_video_seconds,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "daily_credit_limit": settings.gnani_daily_limit,
        "used_daily_credits": int(used),
        "remaining_daily_credits": max(0, settings.gnani_daily_limit - int(used)),
        "max_credits_per_job": settings.max_credits_per_job,
        "numeral_style": settings.numeral_style,
        "low_trust_verify_langs": settings.low_trust_verify_langs,
    }


@app.get("/api/settings")
async def read_settings() -> dict:
    """Expose effective non-secret settings; environment remains the source of truth."""
    config = await get_config()
    return {
        **config,
        "max_credits_per_job": settings.max_credits_per_job,
        "max_video_seconds": settings.max_video_seconds,
        "numeral_style": settings.numeral_style,
        "settings_source": "server environment",
        "editable": False,
    }


@app.get("/api/projects/{project_id}/summary")
async def project_summary(project_id: str) -> dict:
    project = project_row(project_id)
    with db.connect() as connection:
        clip_count = connection.execute("SELECT COUNT(*) FROM clips WHERE project_id=?", (project_id,)).fetchone()[0]
        count_rows = connection.execute(
            "SELECT c.start_s,c.end_s,d.id,d.lang,d.status,d.auto_status,d.translation,d.selected_take_id,d.audio_path,d.duration_s,d.audio_stale,d.user_verified,d.verified_fingerprint,d.verified_at,d.verify_override,d.verify_override_note,d.timing_offset_s "
            "FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id WHERE c.project_id=?", (project_id,),
        ).fetchall()
        jobs = connection.execute(
            "SELECT id,kind,lang,state,stage,done,total,error,created_at,updated_at "
            "FROM jobs WHERE project_id=? ORDER BY created_at DESC", (project_id,)
        ).fetchall()
        checkpoint_row = connection.execute(
            "SELECT checkpoint_json FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,)
        ).fetchone()
        language_counts = connection.execute(
            "SELECT d.lang,COUNT(*) AS total,SUM(CASE WHEN TRIM(d.translation)<>'' THEN 1 ELSE 0 END) AS translated "
            "FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id WHERE c.project_id=? GROUP BY d.lang", (project_id,),
        ).fetchall()
    checkpoint = json.loads(checkpoint_row["checkpoint_json"] or "{}") if checkpoint_row else {}
    counts = {"total": int(clip_count), "green": 0, "yellow": 0, "red": 0}
    for count_row in count_rows:
        status = count_row["auto_status"] or count_row["status"] or "pending"
        # Approval is a user decision tied to text, selected take and placement.
        # Media rebuilds and file mtimes do not alter that fingerprint.
        if count_row["user_verified"] and count_row["verified_fingerprint"] == verification_fingerprint(count_row):
            status = "green"
        elif status == "green":
            # Machine checks are advisory; only an explicit user decision is Verified.
            status = "yellow"
        if status not in {"green", "yellow", "red"}:
            status = "yellow"
        counts[status] += 1
    expected = len((checkpoint.get("term_fixes") or {}).get("cleaned_rows") or [])
    language_status = {}
    counts_by_language = {row["lang"]: row for row in language_counts if row["lang"]}
    for lang in json.loads(project["target_langs_json"]):
        row = counts_by_language.get(lang)
        count = int(row["total"] or 0) if row else 0
        translated = int(row["translated"] or 0) if row else 0
        complete = bool((expected or clip_count) and count == (expected or clip_count) and translated == count)
        reason = (checkpoint.get("language_errors") or {}).get(lang)
        lang_rows = [item for item in count_rows if item["lang"] == lang]
        speech_needed_s = sum(max(0.0, float(item["duration_s"] or 0)) for item in lang_rows)
        time_available_s = float(project["duration_s"] or 0)
        language_status[lang] = {
            "status": "complete" if complete else "failed" if reason and project["status"] == "failed" else "processing" if project["status"] == "processing" else "pending",
            "completed_clips": translated, "total_clips": expected or clip_count, "error": reason,
            "timing_health": {
                "speech_needed_s": round(speech_needed_s, 3),
                "time_available_s": round(time_available_s, 3),
                "shorter_translation_suggested": speech_needed_s > time_available_s + 0.1,
            },
        }
    return {
        "project": {**project, "target_langs": json.loads(project["target_langs_json"])},
        "clips": {"total": int(clip_count), "dub_rows": sum(1 for row in count_rows if row["status"] is not None),
                  **{key: counts[key] for key in ("green", "yellow", "red")}},
        "language_status": language_status,
        "jobs": [dict(row) for row in jobs],
        "active_job": next((dict(row) for row in jobs if row["state"] in {"pending", "running"}), None),
    }


@app.get("/api/term-memory")
async def list_global_terms(q: str = "", treatment: str | None = None) -> dict:
    clauses, params = [], []
    if q.strip():
        clauses.append("(term LIKE ? OR note LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
    if treatment in {"keep", "transliterate", "translate"}:
        clauses.append("treatment=?")
        params.append(treatment)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM global_terms{where} ORDER BY term COLLATE NOCASE", params
        ).fetchall()
    return {"terms": [term_row_payload(row) for row in rows], "count": len(rows)}


@app.post("/api/term-memory", status_code=201)
async def create_global_term(body: GlobalTermRequest) -> dict:
    try:
        term = TermSheet.model_validate({"terms": [body.model_dump()]}).terms[0]
    except Exception as error:
        raise HTTPException(422, f"Invalid term: {error}") from error
    term_id = new_id()
    with db.connect() as connection:
        try:
            connection.execute(
                "INSERT INTO global_terms(id,term,type,treatment,locked,targets_json,note,rule_type,heard_variants_json,spoken_forms_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (term_id, term.term.strip(), term.type, term.treatment, int(term.locked), json.dumps(term.targets, ensure_ascii=False), body.note, term.rule_type, json.dumps(term.heard_variants, ensure_ascii=False), json.dumps(term.spoken_forms, ensure_ascii=False)),
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise HTTPException(409, "This term and type are already in term memory") from error
            raise
        row = connection.execute("SELECT * FROM global_terms WHERE id=?", (term_id,)).fetchone()
    return term_row_payload(row)


@app.put("/api/term-memory/{term_id}")
async def update_global_term(term_id: str, body: GlobalTermRequest) -> dict:
    try:
        term = TermSheet.model_validate({"terms": [body.model_dump()]}).terms[0]
    except Exception as error:
        raise HTTPException(422, f"Invalid term: {error}") from error
    with db.connect() as connection:
        exists = connection.execute("SELECT id FROM global_terms WHERE id=?", (term_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Term not found")
        try:
            connection.execute(
                "UPDATE global_terms SET term=?,type=?,treatment=?,locked=?,targets_json=?,note=?,rule_type=?,heard_variants_json=?,spoken_forms_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (term.term.strip(), term.type, term.treatment, int(term.locked), json.dumps(term.targets, ensure_ascii=False), body.note, term.rule_type, json.dumps(term.heard_variants, ensure_ascii=False), json.dumps(term.spoken_forms, ensure_ascii=False), term_id),
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise HTTPException(409, "This term and type are already in term memory") from error
            raise
        row = connection.execute("SELECT * FROM global_terms WHERE id=?", (term_id,)).fetchone()
    return term_row_payload(row)


@app.delete("/api/term-memory/{term_id}")
async def delete_global_term(term_id: str) -> dict:
    with db.connect() as connection:
        cursor = connection.execute("DELETE FROM global_terms WHERE id=?", (term_id,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Term not found")
    return {"deleted": True, "id": term_id}


@app.get("/api/usage")
async def usage_ledger(
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    project_id: str | None = None, kind: str | None = None,
) -> dict:
    clauses, params = [], []
    if project_id:
        clauses.append("l.project_id=?")
        params.append(project_id)
    if kind:
        clauses.append("l.kind=?")
        params.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.connect() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM credit_ledger l{where}", params).fetchone()[0]
        rows = connection.execute(
            "SELECT l.*,p.name AS project_name FROM credit_ledger l "
            "LEFT JOIN projects p ON p.id=l.project_id" + where +
            " ORDER BY l.created_at DESC,l.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        spent = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger").fetchone()[0]
        daily = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
    entries = [{**dict(row), "date": row["created_at"], "credits": row["est_credits"]} for row in rows]
    remaining = max(0, settings.gnani_daily_limit-int(daily))
    return {
        "entries": entries, "ledger": entries, "total": int(total), "limit": limit, "offset": offset,
        "totals": {"all_time_credits": int(spent), "used_daily_credits": int(daily), "daily_credit_limit": settings.gnani_daily_limit, "remaining_daily_credits": max(0, settings.gnani_daily_limit-int(daily))},
        "spent": int(daily), "remaining_credits": remaining, "credit_limit": settings.gnani_daily_limit,
        "by_language": {},
        "credit_values_are": "estimated credits recorded before job execution",
    }


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT p.*,j.state AS job_state,j.stage AS job_stage,j.error AS job_error "
            "FROM projects p LEFT JOIN jobs j ON j.id=(SELECT id FROM jobs WHERE project_id=p.id ORDER BY created_at DESC LIMIT 1) "
            "ORDER BY p.created_at DESC"
        ).fetchall()
    return [{**dict(row), "target_langs": json.loads(row["target_langs_json"])} for row in rows]


@app.get("/api/projects/summary")
async def projects_summary() -> dict:
    with db.connect() as connection:
        projects = connection.execute("SELECT status,COUNT(*) AS count FROM projects GROUP BY status").fetchall()
        jobs = connection.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state").fetchall()
        clips = connection.execute(
            "SELECT d.status,COUNT(*) AS count FROM dubs d GROUP BY d.status"
        ).fetchall()
        total_projects = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        total_clips = connection.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    project_statuses = {row["status"]: int(row["count"]) for row in projects}
    job_statuses = {row["state"]: int(row["count"]) for row in jobs}
    clip_statuses = {row["status"]: int(row["count"]) for row in clips if row["status"]}
    return {
        "project_count": int(total_projects), "clip_count": int(total_clips),
        "project_statuses": project_statuses, "job_statuses": job_statuses,
        "clip_statuses": {"green": clip_statuses.get("green", 0), "yellow": clip_statuses.get("yellow", 0), "red": clip_statuses.get("red", 0)},
        "active_jobs": sum(job_statuses.get(state, 0) for state in ("pending", "running")),
    }


@app.post("/api/projects")
async def create_project(body: ProjectCreate) -> dict:
    valid = set(settings.enabled_langs)
    if not body.target_langs or set(body.target_langs) - valid:
        raise HTTPException(422, "Choose at least one enabled target language")
    if body.source_lang != "auto" and body.source_lang not in LANGUAGES:
        raise HTTPException(422, "Unsupported source language")
    if body.translation_style not in {"natural", "pure"}:
        raise HTTPException(422, "Translation style must be natural or pure")
    project_id = new_id()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO projects(id,name,source_lang,target_langs_json,translation_style) VALUES(?,?,?,?,?)",
            (project_id, body.name, body.source_lang, json.dumps(body.target_langs), body.translation_style),
        )
    return {"id": project_id, "name": body.name, "status": "created", "translation_style": body.translation_style}


@app.patch("/api/projects/{project_id}/name")
async def rename_project(project_id: str, body: ProjectRename, request: Request) -> dict:
    _require_privacy_delete_password(request)
    project_row(project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Project name cannot be empty")
    with db.connect() as connection:
        connection.execute("UPDATE projects SET name=? WHERE id=?", (name, project_id))
    return {"id": project_id, "name": name}


@app.post("/api/projects/{project_id}/translation-style/preview")
async def preview_translation_style(project_id: str, body: TranslationStyleChange) -> dict:
    project = project_row(project_id)
    if body.style not in {"natural", "pure"}:
        raise HTTPException(422, "Translation style must be natural or pure")
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT d.lang,d.human_edited,d.timing_locked,d.user_verified,d.status,d.reasons_json FROM clips c JOIN dubs d ON d.clip_id=c.id WHERE c.project_id=?",
            (project_id,),
        ).fetchall()
        clip_count = connection.execute("SELECT COUNT(*) FROM clips WHERE project_id=?", (project_id,)).fetchone()[0]
        latest = connection.execute(
            "SELECT j.voice_id,j.content_type,j.voice_mode,j.stock_voice FROM jobs j LEFT JOIN voices v ON v.id=j.voice_id WHERE j.project_id=? AND ((j.voice_mode='clone' AND v.consent_ok=1) OR j.voice_mode='stock') ORDER BY j.rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    eligible = eligible_style_switch_rows(rows)
    warnings = sum(1 for row in eligible if row["status"] in {"yellow", "red"} or json.loads(row["reasons_json"] or "[]"))
    credits = estimated_job_credits(float(project.get("duration_s") or 0) * len(eligible) / max(1, clip_count), 1)
    return {"style": body.style, "current_style": project.get("translation_style") or "natural", "eligible_clips": len(eligible),
            "automatic_warnings": warnings, "estimated_credits": credits, "can_change": bool(latest),
            "languages": sorted({row["lang"] for row in eligible})}


@app.post("/api/projects/{project_id}/translation-style", status_code=202)
async def change_translation_style(request: Request, project_id: str, body: TranslationStyleChange, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    preview = await preview_translation_style(project_id, body)
    if not body.confirm:
        raise HTTPException(409, {"error": "confirmation_required", **preview})
    if not preview["can_change"]:
        raise HTTPException(409, "A completed voice job is required before changing translation style")
    if settings.max_credits_per_job and preview["estimated_credits"] > settings.max_credits_per_job:
        raise HTTPException(429, f"Estimated style change cost {preview['estimated_credits']} exceeds the per-job safety cap")
    with db.connect() as connection:
        if connection.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('pending','running') LIMIT 1", (project_id,)).fetchone():
            raise HTTPException(409, "Wait for the current project job to finish before changing style")
        latest = connection.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()
        used_today = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
        if used_today + preview["estimated_credits"] > settings.gnani_daily_limit:
            raise HTTPException(429, "Daily Gnani safety cap reached; try again tomorrow")
        rows = connection.execute("SELECT c.idx,d.lang,d.human_edited,d.timing_locked,d.user_verified FROM clips c JOIN dubs d ON d.clip_id=c.id WHERE c.project_id=?", (project_id,)).fetchall()
        grouped: dict[str, list[int]] = {}
        for row in rows:
            if style_switch_eligible(row):
                grouped.setdefault(row["lang"], []).append(int(row["idx"]))
        job_id = new_id()
        checkpoint = json.dumps({"style_clip_indices": grouped})
        connection.execute(
            "INSERT INTO jobs(id,project_id,kind,voice_id,content_type,translation_style,voice_mode,stock_voice,state,stage,total,checkpoint_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, project_id, "style_switch", latest["voice_id"], latest["content_type"], body.style, latest["voice_mode"], latest["stock_voice"], "pending", "queued", sum(map(len, grouped.values())), checkpoint),
        )
        connection.execute("UPDATE projects SET translation_style=?,status='processing' WHERE id=?", (body.style, project_id))
        connection.execute("INSERT INTO credit_ledger(id,project_id,kind,units,est_credits) VALUES(?,?,?,?,?)", (new_id(), project_id, "translation-style-estimate", len(rows), preview["estimated_credits"]))
    background.add_task(schedule_job, job_id)
    return {"job_id": job_id, "state": "pending", **preview}


@app.put("/api/projects/{project_id}/video")
async def upload_video(project_id: str, video: UploadFile = File(...)) -> dict:
    project = project_row(project_id)
    suffix = Path(safe_upload_name(video.filename)).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise HTTPException(415, "Upload an MP4, MOV, or WebM video")
    upload_size = getattr(video, "size", None)
    if upload_size is not None:
        if upload_size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
        ensure_storage_room(project_id, upload_size)
    folder = DATA_ROOT / "uploads" / project_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"source{suffix}"
    temporary = temporary_path(path)
    size = 0
    try:
        with temporary.open("wb") as target:
            while block := await video.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
                if upload_size is None:
                    ensure_storage_room(project_id, size)
                target.write(block)
        commit_temporary(temporary, path)
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        if is_storage_full(error):
            raise StorageFullError() from error
        raise
    finally:
        temporary.unlink(missing_ok=True)
    try:
        duration, has_video = probe_video(path)
    except Exception as error:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"Video could not be read: {error}") from error
    if not has_video:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "The upload does not contain a video stream")
    if duration > settings.max_video_seconds:
        path.unlink(missing_ok=True)
        raise HTTPException(413, f"Video is {duration:.1f}s; maximum is {settings.max_video_seconds}s")
    preview_notice = "Preparing a browser-compatible preview in the background."
    with db.connect() as connection:
        connection.execute(
            "UPDATE projects SET video_path=?,original_video_path=?,preview_path=NULL,preview_status='pending',preview_error=NULL,duration_s=?,status='uploaded' WHERE id=?",
            (str(path), str(path), duration, project_id),
        )
    queue_project_preview(project_id)
    return {"filename": safe_upload_name(video.filename), "duration_s": duration, "project_id": project_id,
            "preview_notice": preview_notice, "preview_status": "pending"}


@app.post("/api/media/browser-preview")
async def convert_browser_preview(request: Request, video: UploadFile = File(...)):
    """Convert a pre-upload selection to a browser-playable preview, then remove it."""
    enforce_ip_limit(request, "previews")
    suffix = Path(safe_upload_name(video.filename)).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise HTTPException(415, "Upload an MP4, MOV, or WebM video")
    upload_size = getattr(video, "size", None)
    if upload_size is not None:
        preflight_upload_space(upload_size)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=DATA_ROOT, prefix="browser-preview-"))
    source = folder / f"source{suffix}"
    size = 0
    try:
        with source.open("wb") as target:
            while block := await video.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
                if upload_size is None:
                    preflight_upload_space(size)
                target.write(block)
        duration, has_video = probe_video(source)
        if not has_video:
            raise HTTPException(400, "The upload does not contain a video stream")
        if duration > settings.max_video_seconds:
            raise HTTPException(413, f"Video exceeds the {settings.max_video_seconds}s limit")
        playable = browser_video_path(source)
        return FileResponse(
            playable, media_type=mimetypes.guess_type(playable.name)[0] or "application/octet-stream", content_disposition_type="inline",
            background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True),
        )
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(folder, ignore_errors=True)
        if is_storage_full(error):
            raise StorageFullError() from error
        LOGGER.exception("Browser preview conversion failed")
        raise HTTPException(422, "This video could not be converted for preview on the server. The original upload is still usable; try a smaller MP4 if preview is required.") from error


@app.get("/api/projects/{project_id}/video")
async def project_video(project_id: str, request: Request):
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(404, "Project video not found")
    # A browser preview is prepared asynchronously. Keep the uploaded original
    # playable while that work runs instead of making the player wait on a 202.
    primary = project.get("preview_path") if project.get("preview_status") == "ready" else project.get("original_video_path") or project["video_path"]
    fallback = project.get("original_video_path") or project["video_path"]
    failures = []
    candidates = list(dict.fromkeys(path for path in (primary, fallback) if path))
    for candidate in candidates:
        path = Path(candidate)
        try:
            _validate_media_file(path)
            return media_response(path, request)
        except Exception as error:
            _log_media_failure(path, error)
            failures.append(str(error)[:300])
    reason = project.get("preview_error") or "; ".join(failures) or "No browser preview is available."
    return JSONResponse({"error": "media_unavailable", "reason": reason}, status_code=409)


@app.get("/api/projects/{project_id}/preview-status")
async def project_preview_status(project_id: str) -> dict:
    project = project_row(project_id)
    return {"status": project.get("preview_status") or "ready", "reason": project.get("preview_error")}


@app.get("/api/projects/{project_id}/thumbnail")
async def project_thumbnail(project_id: str):
    """Serve a cached frame from the original upload for project cards."""
    project = project_row(project_id)
    source_value = project.get("original_video_path") or project.get("video_path")
    if not source_value:
        return Response(status_code=204)
    source = Path(source_value).resolve()
    root = (DATA_ROOT / "uploads" / project_id).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return Response(status_code=204)
    poster = root / "poster.jpg"
    if poster.is_file() and poster.stat().st_size > 0:
        return FileResponse(poster, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    try:
        duration = float(project.get("duration_s") or 0)
        seek_s = min(max(duration * 0.15, 0.25), 3.0)
        await asyncio.to_thread(
            run_atomic_command,
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{seek_s:.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "4", "-an", str(poster)],
            check=True, capture_output=True, timeout=45,
        )
        return FileResponse(poster, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    except Exception as error:
        LOGGER.info("Project thumbnail unavailable id=%s reason=%s", project_id, str(error)[:250])
        return Response(status_code=204)


@app.post("/api/projects/{project_id}/preview/retry", status_code=202)
async def retry_project_preview(project_id: str) -> dict:
    project = project_row(project_id)
    if not project.get("original_video_path") and not project.get("video_path"):
        raise HTTPException(404, "Project video not found")
    with db.connect() as connection:
        connection.execute("UPDATE projects SET preview_status='pending',preview_error=NULL WHERE id=?", (project_id,))
    queue_project_preview(project_id)
    return {"status": "pending"}


@app.get("/api/projects/{project_id}/voice-reference-preview")
async def voice_reference_preview(project_id: str, request: Request):
    """Return an audio-only preview of the speaker sample extracted from a project video."""
    enforce_ip_limit(request, "previews")
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(404, "Project video not found")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=DATA_ROOT, prefix="voice-preview-") as folder:
            sample = Path(folder) / "reference.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(project["video_path"]),
                 "-map", "0:a:0", "-vn", "-t", "30", "-ac", "1", "-ar", "24000", str(sample)],
                check=True, capture_output=True,
            )
            audio = sample.read_bytes()
            duration = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                 "default=noprint_wrappers=1:nokey=1", str(sample)],
                check=True, capture_output=True, text=True,
            ).stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise HTTPException(422, "Could not extract an audio sample. Check that the video contains speech.") from error
    if duration < 5:
        raise HTTPException(422, "The video needs at least 5 seconds of speech for voice cloning")
    return Response(audio, media_type="audio/wav", headers={
        "Cache-Control": "no-store", "X-Audio-Duration": f"{duration:.2f}",
    })


@app.get("/api/projects/{project_id}/media")
async def dubbed_media(project_id: str, lang: str, request: Request):
    enforce_ip_limit(request, "previews")
    project = project_row(project_id)
    folder = DATA_ROOT / "exports" / project_id
    matches = list(folder.glob(f"*_{lang}*ai-dubbed.mp4"))
    failures: list[str] = []
    if project.get("preview_status") == "pending":
        return JSONResponse({"preview_status": "pending"}, status_code=202, headers={"Retry-After": "2"})
    if matches:
        candidate = matches[0]
        try:
            reason = _browser_transcode_reason(candidate)
            if reason:
                raise ValueError(reason)
            _validate_media_file(candidate)
            return media_response(candidate, request)
        except Exception as error:
            _log_media_failure(candidate, error)
            failures.append("The dubbed video is incomplete or could not be converted.")

    source_value = project.get("preview_path") if project.get("preview_status") == "ready" else project.get("original_video_path") or project.get("video_path")
    source = Path(source_value) if source_value else None
    if source is not None:
        try:
            rebuilt = _rebuild_dubbed_preview(project_id, lang, source, folder)
            reason = _browser_transcode_reason(rebuilt)
            if reason:
                raise ValueError(reason)
            _validate_media_file(rebuilt)
            return media_response(rebuilt, request)
        except Exception as error:
            _log_media_failure(source, error)
            failures.append("The dubbed preview could not be rebuilt from its saved tracks.")

        try:
            _validate_media_file(source)
            return media_response(source, request)
        except Exception as error:
            _log_media_failure(source, error)
            failures.append("The original video preview is unavailable.")

    reason = " ".join(failures) or "The dubbed video and original upload are unavailable."
    return JSONResponse(status_code=409, content={"error": "media_unavailable", "reason": reason})


@app.get("/api/projects/{project_id}/estimate")
async def estimate(project_id: str) -> dict:
    project = project_row(project_id)
    if not project["duration_s"]:
        raise HTTPException(409, "Upload a video to estimate processing")
    targets = json.loads(project["target_langs_json"])
    clips = max(1, math.ceil(project["duration_s"] / 12))
    estimated = estimated_job_credits(project["duration_s"], len(targets))
    with db.connect() as connection:
        used = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
    return {"duration_s": project["duration_s"], "estimated_clips": clips, "languages": len(targets), "estimated_credits": estimated, "remaining_daily_credits": max(0, settings.gnani_daily_limit - int(used))}


@app.get("/api/voices/stock")
async def stock_voices() -> list[dict]:
    return [{"name": name, "description": description, "model": "timbre-v3.0"}
            for name, description in STOCK_VOICES.items()]


@app.post("/api/voices/stock-preview")
async def stock_voice_preview(body: StockVoicePreview, request: Request):
    enforce_ip_limit(request, "previews")
    if body.voice not in STOCK_VOICES:
        raise HTTPException(422, "Choose one of the available Timbre sample voices")
    if body.language not in STOCK_PREVIEW_LINES:
        raise HTTPException(422, "Choose a supported target language")
    try:
        client = create_gnani_client(settings.gnani_mock)
        audio = client.synth_stock(STOCK_PREVIEW_LINES[body.language], body.voice, body.language)
    except Exception as error:
        raise HTTPException(502, f"Gnani voice preview failed: {error}") from error
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/voices/consent-phrase")
async def consent_phrase(session_id: str = "default") -> dict:
    import secrets

    phrase = secrets.choice(CONSENT_PHRASES)
    consent_sessions[session_id] = phrase
    return {"phrase": phrase, "notice": "This confirms a live recording; it does not prove identity."}


@app.post("/api/voices")
async def create_voice(
    request: Request,
    project_id: str = Form(...),
    session_id: str = Form("default"),
    label: str = Form("My voice"),
    consent_audio: UploadFile = File(...),
    reference_audio: UploadFile | None = File(default=None),
) -> dict:
    enforce_ip_limit(request, "voice_clones")
    phrase = consent_sessions.get(session_id)
    if not phrase:
        raise HTTPException(400, "Request a consent phrase and record it live before voice setup")
    consent_bytes = await consent_audio.read()
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(409, "Upload a source video before cloning its speaker")
    voice_id = new_id()
    voice_dir = DATA_ROOT / "voices"
    voice_dir.mkdir(parents=True, exist_ok=True)
    reference_path = voice_dir / f"{voice_id}.wav"
    with tempfile.TemporaryDirectory(dir=voice_dir, prefix="convert-") as folder:
        source = Path(folder) / safe_upload_name(consent_audio.filename)
        consent_wav = Path(folder) / "consent.wav"
        atomic_write_bytes(source, consent_bytes)
        try:
            run_atomic_command(["ffmpeg", "-y", "-v", "error", "-threads", "1", "-i", str(source), "-ac", "1", "-ar", "16000", str(consent_wav)], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            LOGGER.exception("Consent recording conversion failed")
            raise HTTPException(422, "We could not read the consent recording. Please allow microphone access and record the phrase again.") from error
        consent_bytes = consent_wav.read_bytes()
        reference_wav = Path(folder) / "reference.wav"
        if reference_audio:
            reference_source = Path(folder) / safe_upload_name(reference_audio.filename)
            atomic_write_bytes(reference_source, await reference_audio.read())
            reference_input = str(reference_source)
            audio_map = []
        else:
            reference_input = str(project["video_path"])
            audio_map = ["-map", "0:a:0", "-vn"]
        try:
            run_atomic_command(
                ["ffmpeg", "-y", "-v", "error", "-threads", "1", "-i", reference_input, *audio_map,
                 "-t", "30", "-ac", "1", "-ar", "24000", str(reference_wav)],
                check=True, capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            LOGGER.exception("Voice reference extraction failed for project %s", project_id)
            raise HTTPException(422, "Could not extract a voice track from the source video. Check that it contains clear speech.") from error
        try:
            reference_duration = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(reference_wav)],
                check=True, capture_output=True, text=True,
            ).stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as error:
            LOGGER.exception("Voice reference duration probe failed for project %s", project_id)
            raise HTTPException(422, "We could not inspect the extracted voice sample. Please try another video.") from error
        if reference_duration < 5:
            raise HTTPException(422, "The source video needs at least 5 seconds of audio for voice cloning")
        reference_bytes = reference_wav.read_bytes()
    try:
        consent_client = create_gnani_client(settings.gnani_mock)
        transcript = consent_client.stt(consent_bytes, "indic-auto", "prisma-v3.0")
    except Exception as error:
        LOGGER.exception("Gnani could not verify the consent recording")
        raise HTTPException(502, "Gnani could not verify the consent recording right now. Please retry in a moment.") from error
    # Gnani deployments may wrap transcript text under `data`; tolerate both
    # documented response styles and fail as a provider error, never as a 500.
    transcript_text = transcript if isinstance(transcript, str) else ""
    if isinstance(transcript, dict):
        candidate = transcript.get("transcript") or transcript.get("text")
        if not candidate and isinstance(transcript.get("data"), dict):
            candidate = transcript["data"].get("transcript") or transcript["data"].get("text")
        if isinstance(candidate, str):
            transcript_text = candidate
    if not transcript_text.strip():
        LOGGER.error("Gnani consent STT response did not contain transcript text")
        raise HTTPException(502, "Gnani returned no transcript for the consent recording. Please retry the recording.")
    match = ratio(phrase.casefold(), transcript_text.casefold())
    if match < settings.consent_match_threshold:
        raise HTTPException(422, f"Consent phrase did not match (similarity {match:.0f}%)")
    try:
        embedding = consent_client.make_embedding(reference_bytes)
    except Exception as error:
        LOGGER.exception("Gnani voice embedding request failed")
        raise HTTPException(502, "Gnani could not create the voice model right now. Your consent was verified; please retry in a moment.") from error
    try:
        atomic_write_bytes(reference_path, reference_bytes)
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO voices(id,session_id,label,embedding_json,consent_phrase,consent_ok,reference_path) VALUES(?,?,?,?,?,1,?)",
                (voice_id, session_id, label, json.dumps(embedding), phrase, str(reference_path)),
            )
    except Exception as error:
        reference_path.unlink(missing_ok=True)
        LOGGER.exception("Could not save the verified voice model for project %s", project_id)
        raise HTTPException(503, "Consent was verified, but the voice model could not be saved. Please retry.") from error
    consent_sessions.pop(session_id, None)
    return {"id": voice_id, "label": label, "consent_confirmed": True, "message": "Consent confirms a live recording; it does not prove identity."}


@app.delete("/api/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request) -> dict:
    _require_privacy_delete_password(request)
    with db.connect() as connection:
        row = connection.execute("SELECT reference_path FROM voices WHERE id=?", (voice_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Voice not found")
        if row["reference_path"]:
            reference = Path(row["reference_path"]).resolve()
            if reference.is_relative_to(DATA_ROOT.resolve()):
                reference.unlink(missing_ok=True)
        connection.execute("DELETE FROM voices WHERE id=?", (voice_id,))
    return {"deleted": True}


@app.get("/api/voices")
async def list_voices() -> list[dict]:
    """List safe voice metadata; never return stored embeddings or audio paths."""
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT id,label,consent_ok,created_at,reference_path FROM voices ORDER BY created_at DESC"
        ).fetchall()
    return [{"id": row["id"], "label": row["label"], "consent_confirmed": bool(row["consent_ok"]),
             "has_reference": bool(row["reference_path"]), "created_at": row["created_at"]} for row in rows]


@app.delete("/api/projects/{project_id}/audio")
async def delete_project_audio(project_id: str, request: Request) -> dict:
    """Remove source and generated audio/video assets while retaining text and reports."""
    _require_privacy_delete_password(request)
    project = project_row(project_id)
    data_root = DATA_ROOT.resolve()
    upload_dir = (DATA_ROOT / "uploads" / project_id).resolve()
    source_video = Path(project["video_path"]).resolve() if project.get("video_path") else None
    source_video_removed = bool(upload_dir.is_relative_to(data_root) and source_video and source_video.is_relative_to(upload_dir) and source_video.is_file())
    if upload_dir.is_relative_to(data_root) and upload_dir.is_dir():
        shutil.rmtree(upload_dir)

    removed = []
    exports_dir = (DATA_ROOT / "exports" / project_id).resolve()
    if exports_dir.is_relative_to(data_root) and exports_dir.is_dir():
        for path in exports_dir.iterdir():
            if path.is_file() and path.suffix.lower() in {".mp4", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}:
                resolved = path.resolve()
                if resolved.is_relative_to(exports_dir):
                    path.unlink(missing_ok=True)
                    removed.append(path.name)

    with db.connect() as connection:
        audio_rows = connection.execute(
            "SELECT src_audio_path FROM clips WHERE project_id=? AND src_audio_path IS NOT NULL", (project_id,)
        ).fetchall()
        dub_rows = connection.execute(
            "SELECT d.audio_path FROM dubs d JOIN clips c ON c.id=d.clip_id "
            "WHERE c.project_id=? AND d.audio_path IS NOT NULL", (project_id,)
        ).fetchall()
        for row in [*audio_rows, *dub_rows]:
            candidate = Path(row[0]).resolve()
            if candidate.is_relative_to(data_root) and candidate.is_file():
                candidate.unlink(missing_ok=True)
                removed.append(candidate.name)
        connection.execute("UPDATE clips SET src_audio_path=NULL WHERE project_id=?", (project_id,))
        connection.execute(
            "UPDATE dubs SET audio_path=NULL,audio_hash=NULL,duration_s=NULL WHERE clip_id IN (SELECT id FROM clips WHERE project_id=?)",
            (project_id,),
        )
        connection.execute("UPDATE projects SET video_path=NULL,status='audio_deleted' WHERE id=?", (project_id,))
    return {"deleted": True, "project_id": project_id, "source_video_removed": source_video_removed,
            "removed_generated_files": removed, "transcripts_translations_terms_and_reports_retained": True}


@app.get("/api/projects/{project_id}/termsheet")
async def get_termsheet(project_id: str) -> dict:
    project_row(project_id)
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM terms WHERE project_id=?", (project_id,)).fetchall()
    return {"terms": [term_row_payload(row) for row in rows], "idioms": []}


@app.put("/api/projects/{project_id}/termsheet")
async def approve_terms(project_id: str, body: TermsUpdate) -> dict:
    project_row(project_id)
    sheet = TermSheet.model_validate({"terms": body.terms, "idioms": body.idioms})
    with db.connect() as connection:
        connection.execute("DELETE FROM terms WHERE project_id=?", (project_id,))
        for term in sheet.terms:
            connection.execute(
                "INSERT INTO terms(id,project_id,term,type,treatment,locked,targets_json,source,rule_type,heard_variants_json,spoken_forms_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (new_id(), project_id, term.term, term.type, term.treatment, int(term.locked), json.dumps(term.targets, ensure_ascii=False), "approved", term.rule_type, json.dumps(term.heard_variants, ensure_ascii=False), json.dumps(term.spoken_forms, ensure_ascii=False)),
            )
            if term.save_global:
                connection.execute(
                    "INSERT INTO global_terms(id,term,type,treatment,locked,targets_json,note,rule_type,heard_variants_json,spoken_forms_json) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(term,type) DO UPDATE SET treatment=excluded.treatment,locked=excluded.locked,targets_json=excluded.targets_json,note=excluded.note,rule_type=excluded.rule_type,heard_variants_json=excluded.heard_variants_json,spoken_forms_json=excluded.spoken_forms_json,updated_at=CURRENT_TIMESTAMP",
                    (new_id(), term.term, term.type, term.treatment, int(term.locked), json.dumps(term.targets, ensure_ascii=False), "Saved from project term sheet", term.rule_type, json.dumps(term.heard_variants, ensure_ascii=False), json.dumps(term.spoken_forms, ensure_ascii=False)),
                )
    return {"approved": True, "terms": len(sheet.terms)}


@app.post("/api/projects/{project_id}/terms/apply")
async def apply_term(project_id: str, body: TermApply) -> dict:
    project_row(project_id)
    with db.connect() as connection:
        term = connection.execute("SELECT * FROM terms WHERE id=? AND project_id=?", (body.term_id, project_id)).fetchone()
        if term is None:
            raise HTTPException(404, "Term not found")
        clips = connection.execute("SELECT id,src_text FROM clips WHERE project_id=?", (project_id,)).fetchall()
        matching = [dict(clip) for clip in clips if term["term"] in clip["src_text"]]
        if body.preview:
            return {"preview": True, "matches": len(matching), "replacement": body.replacement}
        targets = json.loads(term["targets_json"])
        targets[project_row(project_id)["source_lang"]] = body.replacement
        connection.execute("UPDATE terms SET targets_json=? WHERE id=?", (json.dumps(targets, ensure_ascii=False), body.term_id))
    return {"preview": False, "updated": True, "matched_clips": len(matching)}


@app.get("/api/projects/{project_id}/clips")
async def get_clips(project_id: str, lang: str) -> list[dict]:
    project_row(project_id)
    with db.connect() as connection:
        rows = connection.execute(
        "SELECT c.*,d.id AS dub_id,d.selected_take_id,d.translation,d.draft_translation,d.status,d.auto_status,d.reasons_json,d.attempts_json,d.audio_path,d.audio_stale,d.human_edited,d.user_verified,d.verified_fingerprint,d.verified_at,d.verify_override,d.verify_override_note,d.timing_offset_s,d.timing_locked,d.timing_speedup,d.trim_head_s,d.trim_tail_s,d.pause_tightening,d.duration_s AS dub_duration_s "
            "FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=? ORDER BY c.idx",
            (lang, project_id),
        ).fetchall()
    review_by_clip: dict[int, dict] = {}
    report_path = _language_artifacts(project_id, lang).get("report")
    if report_path:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            review_by_clip = {int(item.get("clip", 0)) - 1: item for item in payload.get("clips", []) if item.get("clip") is not None}
        except (OSError, ValueError, json.JSONDecodeError):
            review_by_clip = {}
    result = []
    for row in rows:
        item = dict(row)
        automatic_status = item.get("auto_status") or item.get("status") or "pending"
        audio_path = item.pop("audio_path", None)
        audio_present = bool(audio_path and Path(audio_path).is_file())
        audio_version = Path(audio_path).stat().st_mtime_ns if audio_present else ""
        item["audio_url"] = f"/api/clips/{item['id']}/dubs/{lang}/audio?v={audio_version}" if audio_present else None
        item["waveform_peaks"] = _audio_peaks(Path(audio_path)) if audio_present else []
        item["audio_stale"] = bool(item.get("audio_stale", False))
        verification_audio = verification_audio_path(project_id, lang, audio_path)
        item["verification_available"] = bool(verification_audio and not item["audio_stale"])
        item["timeline_start_s"] = float(item["start_s"]) + float(item.get("timing_offset_s") or 0)
        item["timing_locked"] = bool(item.get("timing_locked", False))
        item["audio_removed"] = bool(audio_path and not audio_present)
        verified = bool(item.get("user_verified") and item.get("verified_fingerprint") == verification_fingerprint(item))
        item["automatic_status"] = automatic_status
        item["user_verified"] = verified
        item["verify_override"] = bool(verified and item.get("verify_override"))
        item["verified_at"] = item.get("verified_at") if verified else None
        item["status"] = "green" if verified else ("yellow" if automatic_status == "green" else automatic_status)
        item["status"] = "yellow" if item["status"] == "pending" else item["status"]
        attempts = json.loads(row["attempts_json"] or "[]")
        attempt_root = (DATA_ROOT / "exports" / project_id).resolve()
        for index, attempt in enumerate(attempts):
            old_key = attempt.pop("old_audio_key", None)
            new_key = attempt.pop("new_audio_key", None)
            def attempt_exists(key: str | None) -> bool:
                path = (attempt_root / key).resolve() if key else None
                return bool(path and path.is_relative_to(attempt_root) and path.is_file())
            attempt["old_audio_url"] = f"/api/clips/{item['id']}/dubs/{lang}/attempts/{index}/previous" if attempt_exists(old_key) else None
            attempt["new_audio_url"] = f"/api/clips/{item['id']}/dubs/{lang}/attempts/{index}/updated" if attempt_exists(new_key) else None
        item.update({
            "reasons": json.loads(row["reasons_json"] or "[]"),
            "attempts": attempts,
            "source_raw": review_by_clip.get(int(row["idx"]), {}).get("source_raw"),
            "stt_review": review_by_clip.get(int(row["idx"]), {}).get("stt_review", {}),
            "slot_s": review_by_clip.get(int(row["idx"]), {}).get("slot_s", float(row["end_s"]) - float(row["start_s"])),
            "spoken_duration_s": review_by_clip.get(int(row["idx"]), {}).get("final_audio_duration_s", row["dub_duration_s"]),
            "timing_speedup": float(item.get("timing_speedup") or review_by_clip.get(int(row["idx"]), {}).get("timing_speedup", 1.0)),
            "trim_head_s": float(item.get("trim_head_s") or 0.0),
            "trim_tail_s": float(item.get("trim_tail_s") or 0.0),
            "pause_tightening": bool(item.get("pause_tightening")),
            "timing_shorten_required": bool(review_by_clip.get(int(row["idx"]), {}).get("timing_shorten_required", False)),
            "duration_budget_chars": review_by_clip.get(int(row["idx"]), {}).get("duration_budget_chars"),
            "withheld": bool(review_by_clip.get(int(row["idx"]), {}).get("withheld", False)),
        })
        item["timeline_end_s"] = review_by_clip.get(int(row["idx"]), {}).get(
            "scheduled_end_s",
            item["timeline_start_s"] + float(item.get("spoken_duration_s") or 0) / max(1.0, float(item.get("timing_speedup") or 1.0)),
        )
        reason_texts = item["reasons"]
        reason_blob = " ".join(reason_texts).casefold()
        if "text changed" in reason_blob:
            reason_code, reason_text, fix_action = "text_changed", "Text changed, verify again.", "Re-speak this clip"
        elif "withheld" in reason_blob or "exceeds its safe slot" in reason_blob or "slot" in reason_blob and "speech" in reason_blob:
            reason_code, reason_text, fix_action = "too_long_for_slot", "Speech is too long for its time slot.", "Shorten the translation"
        elif "number" in reason_blob or "name" in reason_blob or "term missing" in reason_blob:
            reason_code, reason_text, fix_action = "number_or_name_missing", "A number or name may be missing.", "Check the translation"
        elif "disagree" in reason_blob:
            reason_code, reason_text, fix_action = "transcript_models_disagree", "The transcript checks do not agree.", "Review the source text"
        elif "transcript" in reason_blob or "uncertain source" in reason_blob or "source cleanup" in reason_blob:
            reason_code, reason_text, fix_action = "source_looks_wrong", "The source transcript may be wrong.", "Fix the source text"
        elif "meaning" in reason_blob:
            reason_code, reason_text, fix_action = "source_looks_wrong", "The source meaning needs review.", "Fix the source text"
        else:
            reason_code = "review_needed"
            reason_text = "" if automatic_status == "green" else "This clip needs a quick review."
            fix_action = "" if automatic_status == "green" else "Review this clip"
        item.update({"reason_code": reason_code, "reason_text": reason_text, "fix_action": fix_action})
        item.pop("attempts_json", None)
        item.pop("reasons_json", None)
        item.pop("dub_id", None)
        item.pop("verified_fingerprint", None)
        item.pop("dub_duration_s", None)
        result.append(item)
    return result


@app.get("/api/clips/{clip_id}/dubs/{lang}/attempts/{attempt_index}/{take}")
async def clip_attempt_audio(clip_id: str, lang: str, attempt_index: int, take: str):
    if take not in {"previous", "updated"}:
        raise HTTPException(404, "Attempt audio is not available")
    with db.connect() as connection:
        row = connection.execute(
            "SELECT d.attempts_json,c.project_id FROM dubs d JOIN clips c ON c.id=d.clip_id WHERE c.id=? AND d.lang=?",
            (clip_id, lang),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Attempt audio is not available")
    try:
        attempts = json.loads(row["attempts_json"] or "[]")
        key = attempts[attempt_index].get("old_audio_key" if take == "previous" else "new_audio_key")
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        key = None
    root = (DATA_ROOT / "exports" / row["project_id"]).resolve()
    path = (root / key).resolve() if key else None
    if not path or not path.is_file() or not path.is_relative_to(root):
        raise HTTPException(404, "Attempt audio is not available")
    return FileResponse(path, media_type="audio/wav", filename=path.name, headers={"Cache-Control": "no-store"})


@app.get("/api/clips/{clip_id}/dubs/{lang}/audio")
async def clip_dub_audio(clip_id: str, lang: str):
    with db.connect() as connection:
        row = connection.execute(
            "SELECT d.audio_path FROM dubs d JOIN clips c ON c.id=d.clip_id WHERE c.id=? AND d.lang=?",
            (clip_id, lang),
        ).fetchone()
    if row is None or not row["audio_path"]:
        raise HTTPException(404, "Clip audio is not available")
    path = Path(row["audio_path"]).resolve()
    export_root = (DATA_ROOT / "exports").resolve()
    if not path.is_file() or not path.is_relative_to(export_root):
        raise HTTPException(404, "Clip audio is not available")
    return FileResponse(path, media_type="audio/wav", filename=path.name, headers={"Cache-Control": "no-store"})


@app.patch("/api/clips/{clip_id}/dubs/{lang}")
async def edit_dub(clip_id: str, lang: str, body: DubEdit) -> dict:
    with db.connect() as connection:
        clip = connection.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            raise HTTPException(404, "Clip not found")
        term_rows = connection.execute("SELECT * FROM terms WHERE project_id=?", (clip["project_id"],)).fetchall()
    sheet = TermSheet.model_validate({"terms": [term_row_payload(row) for row in term_rows]})
    # Character count is only a rough guide for Indic text and mixed-script names.
    # The real pass/fail timing check happens against generated speech duration.
    checks = run_checks(clip["src_text"], body.translation, lang, sheet)
    reasons = [f"{item['check']}: {item['detail']}" for item in checks]
    status = "red" if checks else "yellow"
    reasons.append("Text changed; verify again.")
    with db.connect() as connection:
        existing = connection.execute("SELECT draft_translation,translation,status,reasons_json,human_edited,audio_stale FROM dubs WHERE clip_id=? AND lang=?", (clip_id, lang)).fetchone()
        if existing and existing["translation"] == body.translation:
            return {"status": existing["status"], "reasons": json.loads(existing["reasons_json"] or "[]"),
                    "human_edited": bool(existing["human_edited"]), "audio_stale": bool(existing["audio_stale"])}
        draft_translation = (existing["draft_translation"] or existing["translation"]) if existing else body.translation
        connection.execute(
        "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,auto_status,reasons_json,human_edited,audio_path,duration_s,audio_stale) VALUES(?,?,?,?,?,?,?,?,1,NULL,NULL,1) "
            "ON CONFLICT(clip_id,lang) DO UPDATE SET translation=excluded.translation,status=excluded.status,auto_status=excluded.status,reasons_json=excluded.reasons_json,human_edited=1,audio_stale=1,user_verified=0,verified_fingerprint=NULL,verified_at=NULL,verify_override=0,verify_override_note=NULL,updated_at=CURRENT_TIMESTAMP",
            (new_id(), clip_id, lang, body.translation, draft_translation, status, status, json.dumps(reasons, ensure_ascii=False)),
        )
    return {"status": status, "reasons": reasons, "human_edited": True}


@app.post("/api/clips/{clip_id}/dubs/{lang}/verify")
async def verify_dub_clip(clip_id: str, lang: str, body: VerifyDecision | None = None) -> dict:
    """Record the user's approval of the current translation, take, and placement."""
    with db.connect() as connection:
        row = connection.execute(
            "SELECT d.id,d.selected_take_id,d.status,d.auto_status,d.reasons_json,d.audio_path,d.audio_stale,d.translation,d.timing_offset_s,d.verify_override,c.project_id,c.idx,c.start_s,c.end_s,"
            "p.status AS project_status,p.duration_s AS video_duration "
            "FROM dubs d JOIN clips c ON c.id=d.clip_id JOIN projects p ON p.id=c.project_id "
            "WHERE c.id=? AND d.lang=?", (clip_id, lang),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Dub not found")
        reasons = json.loads(row["reasons_json"] or "[]")
        reason_blob = " ".join(str(reason) for reason in reasons).casefold()
        truly_cannot_fit = row["status"] == "red" and any(
            marker in reason_blob for marker in ("cannot fit", "still overlaps", "speech overlaps", "withheld", "too long for its slot", "exceeds its slot")
        )
        if truly_cannot_fit and not (body and body.confirm_override):
            raise HTTPException(409, "This clip cannot fit its slot. Confirm Verify anyway to approve it with an override note.")
        reasons = json.loads(row["reasons_json"] or "[]")
        automatic_status = row["auto_status"] or row["status"] or "pending"
        fingerprint = verification_fingerprint(row)
        override_note = ((body.note.strip() if body and body.note else "User approved audio despite unresolved fit warning.")
                         if truly_cannot_fit else None)
        connection.execute(
            "UPDATE dubs SET auto_status=COALESCE(auto_status,status),user_verified=1,verified_fingerprint=?,verified_at=CURRENT_TIMESTAMP,verify_override=?,verify_override_note=? WHERE id=?",
            (fingerprint, int(truly_cannot_fit), override_note, row["id"]),
        )

    report_path = _language_artifacts(row["project_id"], lang).get("report")
    if report_path and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for clip in report.get("clips", []):
                if int(clip.get("clip", -1)) == int(row["idx"]) + 1:
                    clip["automatic_status"] = automatic_status
                    clip["status"] = "green"
                    clip["human_verified"] = True
                    clip["verify_override"] = truly_cannot_fit
                    break
            atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        except (OSError, ValueError, json.JSONDecodeError):
            LOGGER.exception("Could not update review report after human verification for clip %s", clip_id)
    clips = await get_clips(row["project_id"], lang)
    updated_clip = next((clip for clip in clips if clip["id"] == clip_id), None)
    counts = {"total": len(clips), "green": 0, "yellow": 0, "red": 0}
    for clip in clips:
        status = clip.get("status") or "yellow"
        counts[status if status in {"green", "yellow", "red"} else "yellow"] += 1
    return {"status": "green", "human_verified": True, "verify_override": truly_cannot_fit,
            "verify_override_note": override_note, "clip_id": clip_id, "lang": lang,
            "clip": updated_clip, "counts": counts}


@app.post("/api/clips/{clip_id}/dubs/{lang}/unverify")
async def unverify_dub_clip(clip_id: str, lang: str) -> dict:
    with db.connect() as connection:
        row = connection.execute("SELECT id,status,auto_status FROM dubs WHERE clip_id=? AND lang=?", (clip_id, lang)).fetchone()
        if row is None:
            raise HTTPException(404, "Dub not found")
        connection.execute("UPDATE dubs SET user_verified=0,verified_fingerprint=NULL,verified_at=NULL,verify_override=0,verify_override_note=NULL WHERE id=?", (row["id"],))
    status = row["auto_status"] or row["status"] or "yellow"
    return {"status": "yellow" if status == "green" else status, "human_verified": False, "clip_id": clip_id, "lang": lang}


def _render_scheduled_track(project_id: str, lang: str, placements: list[dict], duration_s: float) -> Path:
    """Atomically rebuild the single dubbed track used by preview and export."""
    target = _track_artifact_path(project_id, lang, "dubbed")
    if target is None:
        raise FileNotFoundError(f"The saved dubbed track is missing for {lang}")
    with db.connect() as connection:
        audio_rows = connection.execute(
            "SELECT c.id,d.audio_path,d.trim_head_s,d.trim_tail_s FROM clips c JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=?",
            (lang, project_id),
        ).fetchall()
    audio_by_id = {row["id"]: dict(row) for row in audio_rows if row["audio_path"]}
    inputs: list[str] = []
    graph: list[str] = []
    labels: list[str] = []
    for index, placement in enumerate(placements):
        audio_row = audio_by_id.get(placement.get("id"))
        audio_path = Path(audio_row["audio_path"]) if audio_row else None
        if not audio_path or not audio_path.is_file():
            continue
        inputs.extend(["-i", str(audio_path)])
        speedup = max(1.0, float(placement.get("speedup") or 1.0))
        start_ms = max(0, round(float(placement["start_s"]) * 1000))
        trim_head = max(0.0, float(placement.get("trim_head_s", audio_row.get("trim_head_s", 0)) or 0))
        trim_tail = max(0.0, float(placement.get("trim_tail_s", audio_row.get("trim_tail_s", 0)) or 0))
        effective = max(0.01, float(placement["speech_duration_s"]) - trim_head - trim_tail)
        graph.append(f"[{len(labels)}:a]atrim=start={trim_head:.6f}:duration={effective:.6f},asetpts=PTS-STARTPTS,atempo={speedup:.6f},adelay={start_ms}|{start_ms}[a{len(labels)}]")
        labels.append(f"[a{len(labels)}]")
    if not labels:
        raise FileNotFoundError(f"No saved clip takes are available for {lang}")
    graph.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,apad=whole_dur={duration_s:.6f},atrim=duration={duration_s:.6f}[dub]")
    temporary = target.with_name(f".{target.stem}.balance.tmp.wav")
    try:
        run_atomic_command(
            ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[dub]", "-t", f"{duration_s:.6f}", str(temporary)],
            check=True, capture_output=True, timeout=240,
        )
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        commit_temporary(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _timing_state(connection, project_id: str, lang: str) -> dict[str, dict]:
    rows = connection.execute(
        "SELECT d.clip_id,d.timing_offset_s,d.timing_speedup,d.trim_head_s,d.trim_tail_s,d.pause_tightening,d.timing_locked "
        "FROM dubs d JOIN clips c ON c.id=d.clip_id WHERE c.project_id=? AND d.lang=? ORDER BY c.idx",
        (project_id, lang),
    ).fetchall()
    return {row["clip_id"]: {key: row[key] for key in (
        "timing_offset_s", "timing_speedup", "trim_head_s", "trim_tail_s", "pause_tightening", "timing_locked"
    )} for row in rows}


def _record_timing_history(connection, project_id: str, lang: str, before: dict, after: dict) -> None:
    if before == after:
        return
    connection.execute("DELETE FROM timing_history WHERE project_id=? AND lang=? AND state='undone'", (project_id, lang))
    connection.execute(
        "INSERT INTO timing_history(project_id,lang,before_json,after_json,state) VALUES(?,?,?,?, 'applied')",
        (project_id, lang, json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True)),
    )
    connection.execute(
        "DELETE FROM timing_history WHERE project_id=? AND lang=? AND id NOT IN "
        "(SELECT id FROM timing_history WHERE project_id=? AND lang=? ORDER BY id DESC LIMIT 50)",
        (project_id, lang, project_id, lang),
    )


def _audio_peaks(path: Path) -> list[float]:
    cache = path.with_suffix(path.suffix + ".peaks.json")
    try:
        metadata = {"mtime_ns": path.stat().st_mtime_ns, "size": path.stat().st_size}
        if cache.is_file():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("source") == metadata and isinstance(cached.get("peaks"), list):
                return cached["peaks"]
    except (OSError, ValueError, TypeError):
        metadata = None
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            check=True, capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        LOGGER.warning("Could not read waveform peaks from %s: %s", path, error)
        return []
    samples = array("h")
    samples.frombytes(result.stdout[:len(result.stdout) - (len(result.stdout) % 2)])
    bucket = 40  # 200 waveform points per second at 8 kHz
    peaks = [round(max((abs(value) for value in samples[i:i + bucket]), default=0) / 32768, 4)
             for i in range(0, len(samples), bucket)]
    if metadata is not None:
        try:
            atomic_write_text(cache, json.dumps({"source": metadata, "peaks": peaks}, separators=(",", ":")))
        except OSError:
            LOGGER.warning("Could not cache waveform peaks for %s", path)
    return peaks


def _silence_intervals(path: Path) -> tuple[float, list[tuple[float, float]]]:
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip())
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-45dB:d=0.02", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    log = result.stderr or ""
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    intervals: list[tuple[float, float]] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else duration
        intervals.append((max(0.0, start), min(duration, max(start, end))))
    return duration, intervals


def _trim_take_audio(source: Path, target: Path, tighten_pauses: bool) -> float:
    duration, silences = _silence_intervals(source)
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    leading_trim = 0.0
    trailing_end = duration
    if silences and silences[0][0] <= 0.02:
        leading_trim = max(0.0, silences[0][1] - 0.08)
    if silences and silences[-1][1] >= duration - 0.02:
        trailing_end = min(duration, silences[-1][0] + 0.08)
    cursor = leading_trim
    for start, end in silences:
        if start <= 0.02 or end >= duration - 0.02 or not tighten_pauses or end - start <= 0.3:
            continue
        cut_start = max(cursor, start + 0.075)
        cut_end = min(trailing_end, end - 0.075)
        if cut_start > cursor:
            segments.append((cursor, cut_start))
        cursor = max(cursor, cut_end)
    if trailing_end > cursor:
        segments.append((cursor, trailing_end))
    segments = [(start, end) for start, end in segments if end - start > 0.01]
    if not segments:
        raise ValueError("Trim would remove all speech audio")
    filters = []
    if len(segments) > 1:
        filters.append("[0:a]asplit=" + str(len(segments)) + "".join(f"[in{index}]" for index in range(len(segments))))
    for index, (start, end) in enumerate(segments):
        source_label = f"[in{index}]" if len(segments) > 1 else "[0:a]"
        filters.append(f"{source_label}atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[s{index}]")
    if len(segments) == 1:
        filters.append("[s0]anull[out]")
    else:
        filters.append("".join(f"[s{index}]" for index in range(len(segments))) + f"concat=n={len(segments)}:v=0:a=1[out]")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.wav")
    try:
        run_atomic_command([
            "ffmpeg", "-y", "-v", "error", "-i", str(source), "-filter_complex", ";".join(filters),
            "-map", "[out]", "-c:a", "pcm_s16le", str(temporary),
        ], check=True, capture_output=True, timeout=180)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        commit_temporary(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(target)],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip())


@app.post("/api/projects/{project_id}/balance")
@app.post("/api/projects/{project_id}/pack")
async def balance_project_timing(project_id: str, body: BalanceRequest) -> dict:
    project = project_row(project_id)
    if body.lang not in json.loads(project["target_langs_json"]):
        raise HTTPException(404, "Language is not enabled for this project")
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT c.id,c.idx,c.start_s,c.end_s,c.src_text,d.id AS dub_id,d.selected_take_id,d.translation,d.audio_path,d.duration_s,d.human_edited,d.user_verified,d.timing_offset_s,d.timing_locked,d.trim_head_s,d.trim_tail_s,d.pause_tightening "
            "FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=? ORDER BY c.idx",
            (body.lang, project_id),
        ).fetchall()
    if not rows or any(not row["audio_path"] or not Path(row["audio_path"]).is_file() for row in rows):
        raise HTTPException(409, "Generate speech for every clip before balancing timing")
    inputs = [{
        "id": row["id"], "start_s": row["start_s"], "end_s": row["end_s"],
        "speech_duration_s": row["duration_s"] or 0,
        "trim_head_s": row["trim_head_s"] or 0, "trim_tail_s": row["trim_tail_s"] or 0,
        "pause_tightening": bool(row["pause_tightening"]),
        "timing_offset_s": body.timing_offsets.get(row["id"], row["timing_offset_s"] or 0),
        "placed_start_s": float(row["start_s"]) + float(body.timing_offsets.get(row["id"], row["timing_offset_s"] or 0)),
        "manual": bool(row["timing_locked"] or row["id"] in body.timing_offsets or float(row["timing_offset_s"] or 0) != 0),
        "edited": bool(row["human_edited"]), "locked": bool(row["timing_locked"] or row["id"] in body.timing_offsets or float(row["timing_offset_s"] or 0) != 0),
        "translation": row["translation"] or "", "source_text": row["src_text"] or "",
    } for row in rows]
    schedule = schedule_language(inputs, float(project["duration_s"] or 0), max_shift_s=body.max_shift_s,
                                  max_speedup=body.max_speedup, pack_all=body.pack_all)
    if not schedule["valid"]:
        # Never persist a placement set that violates the no-overlap, shift,
        # drift, or video-end invariants. The client can shorten the flagged
        # translation and ask the scheduler again.
        return {"lang": body.lang, **schedule}
    for placement in schedule.get("placements", []):
        placement.setdefault("speedup", 1.0)
        placement.setdefault("trim_head_s", 0.0)
        placement.setdefault("trim_tail_s", 0.0)
        placement.setdefault("pause_tightening", False)
    if not body.apply:
        return {"lang": body.lang, "preview": True, **schedule}
    placement_by_id = {item["id"]: item for item in schedule["placements"]}
    verification_cleared_clips: list[str] = []
    with db.connect() as connection:
        for row in rows:
            placement = placement_by_id[row["id"]]
            offset = float(placement["start_s"]) - float(row["start_s"])
            moved = abs(offset - float(row["timing_offset_s"] or 0))
            if row["user_verified"] and moved < 0.5:
                updated = dict(row)
                updated["timing_offset_s"] = offset
                fingerprint = verification_fingerprint(updated)
                connection.execute(
                    "UPDATE dubs SET timing_offset_s=?,timing_speedup=?,trim_head_s=?,trim_tail_s=?,pause_tightening=?,timing_locked=CASE WHEN ? THEN 1 ELSE timing_locked END,verified_fingerprint=?,updated_at=CURRENT_TIMESTAMP WHERE clip_id=? AND lang=?",
                    (offset, placement["speedup"], placement["trim_head_s"], placement["trim_tail_s"], int(placement["pause_tightening"]), int(row["dub_id"] in body.timing_offsets), fingerprint, row["id"], body.lang),
                )
            elif row["user_verified"]:
                verification_cleared_clips.append(row["id"])
                connection.execute(
                    "UPDATE dubs SET timing_offset_s=?,timing_speedup=?,trim_head_s=?,trim_tail_s=?,pause_tightening=?,timing_locked=CASE WHEN ? THEN 1 ELSE timing_locked END,user_verified=0,verified_fingerprint=NULL,verified_at=NULL,verify_override=0,verify_override_note=NULL,updated_at=CURRENT_TIMESTAMP WHERE clip_id=? AND lang=?",
                    (offset, placement["speedup"], placement["trim_head_s"], placement["trim_tail_s"], int(placement["pause_tightening"]), int(row["dub_id"] in body.timing_offsets), row["id"], body.lang),
                )
            else:
                connection.execute(
                    "UPDATE dubs SET timing_offset_s=?,timing_speedup=?,trim_head_s=?,trim_tail_s=?,pause_tightening=?,timing_locked=CASE WHEN ? THEN 1 ELSE timing_locked END,updated_at=CURRENT_TIMESTAMP WHERE clip_id=? AND lang=?",
                    (offset, placement["speedup"], placement["trim_head_s"], placement["trim_tail_s"], int(placement["pause_tightening"]), int(row["dub_id"] in body.timing_offsets), row["id"], body.lang),
                )
    artifact = _language_artifacts(project_id, body.lang).get("report")
    if artifact and artifact.is_file():
        try:
            report = json.loads(artifact.read_text(encoding="utf-8"))
            for report_clip in report.get("clips", []):
                index = int(report_clip.get("clip", 0)) - 1
                if 0 <= index < len(rows):
                    placement = placement_by_id[rows[index]["id"]]
                    report_clip["timeline_start_s"] = placement["start_s"]
                    report_clip["scheduled_end_s"] = placement["end_s"]
                    report_clip["timing_speedup"] = placement["speedup"]
                    report_clip["timing_shorten_required"] = placement["shorten_required"]
            atomic_write_text(artifact, json.dumps(report, ensure_ascii=False, indent=2))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.exception("Could not persist timing placements for project %s language %s", project_id, body.lang)
    if schedule["valid"]:
        try:
            _render_scheduled_track(project_id, body.lang, schedule["placements"], float(project["duration_s"] or 0))
            source_value = project.get("preview_path") if project.get("preview_status") == "ready" else project.get("original_video_path") or project.get("video_path")
            if source_value:
                rebuilt = _rebuild_dubbed_preview(project_id, body.lang, Path(source_value), DATA_ROOT / "exports" / project_id)
                canonical = next(iter((DATA_ROOT / "exports" / project_id).glob(f"*_{body.lang}_phase3d_ai-dubbed.mp4")), None)
                if canonical:
                    from backend.atomic_files import atomic_copyfile
                    atomic_copyfile(rebuilt, canonical)
        except Exception as error:
            LOGGER.exception("Could not rebuild balanced dub preview for project %s language %s", project_id, body.lang)
            raise HTTPException(500, f"Timing placements saved, but the preview could not be rebuilt: {str(error)[:250]}") from error
    return {"lang": body.lang, "verification_cleared_clips": verification_cleared_clips, **schedule}


@app.get("/api/projects/{project_id}/timeline/{lang}")
async def get_project_timeline(project_id: str, lang: str) -> dict:
    project = project_row(project_id)
    clips = await get_clips(project_id, lang)
    segments = [{"clip_id": clip["id"], "start_s": float(clip["start_s"]),
                 "end_s": float(clip["end_s"]), "text": clip.get("src_text", "")}
                for clip in clips if clip.get("src_text")]
    return {"lang": lang, "video_duration_s": float(project["duration_s"] or 0),
            "clips": clips, "original_speech_segments": segments}


@app.put("/api/projects/{project_id}/timeline/{lang}/placements")
async def put_timeline_placements(project_id: str, lang: str, body: TimelinePlacementRequest) -> dict:
    project = project_row(project_id)
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT c.id,c.start_s,c.end_s,d.duration_s,d.timing_offset_s,d.timing_speedup,d.trim_head_s,d.trim_tail_s,d.pause_tightening,d.timing_locked "
            "FROM clips c JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=? ORDER BY c.idx",
            (lang, project_id),
        ).fetchall()
        if not rows:
            raise HTTPException(404, "No dubbed clips are available for this language")
        row_by_id = {row["id"]: row for row in rows}
        submitted = {item.clip_id: item for item in body.placements}
        if len(submitted) != len(body.placements) or any(key not in row_by_id for key in submitted):
            raise HTTPException(422, "Placement list contains a duplicate or unknown clip")
        placements = []
        video_end = float(project["duration_s"] or 0)
        for row in rows:
            update = submitted.get(row["id"])
            start = float(update.start_s if update else row["start_s"] + (row["timing_offset_s"] or 0))
            speed = float(update.speed_factor if update else row["timing_speedup"] or 1)
            trim_head = float(update.trim_head_s if update else row["trim_head_s"] or 0)
            trim_tail = float(update.trim_tail_s if update else row["trim_tail_s"] or 0)
            duration = float(row["duration_s"] or 0)
            source_start = float(row["start_s"])
            if start < source_start - 1e-6 or start - source_start > 3.000001:
                raise HTTPException(422, f"Clip {row['id']} start must stay at or after its source slot and within 3 seconds")
            if trim_head + trim_tail >= duration - 0.01:
                raise HTTPException(422, f"Clip {row['id']} trims remove all speech")
            end = start + max(0.0, duration - trim_head - trim_tail) / speed
            placements.append({"id": row["id"], "source_start_s": source_start,
                "source_end_s": float(row["end_s"]), "start_s": start, "end_s": end,
                "speech_duration_s": duration, "speedup": speed, "trim_head_s": trim_head,
                "trim_tail_s": trim_tail, "pause_tightening": bool(update.pause_tightening if update else row["pause_tightening"]),
                "shorten_required": False})
        for previous, following in zip(placements, placements[1:]):
            if previous["end_s"] + 0.1 > following["start_s"] + 1e-6:
                raise HTTPException(422, f"Clip {following['id']} overlaps the preceding clip; allow at least 0.1 seconds")
        if placements[-1]["end_s"] > video_end + 1e-6:
            raise HTTPException(422, "The final clip extends beyond the video duration")
        before = _timing_state(connection, project_id, lang)
        for item in placements:
            original = row_by_id[item["id"]]
            update = submitted.get(item["id"])
            offset = item["start_s"] - float(original["start_s"])
            moved = abs(offset - float(original["timing_offset_s"] or 0))
            connection.execute(
                "UPDATE dubs SET timing_offset_s=?,timing_speedup=?,trim_head_s=?,trim_tail_s=?,pause_tightening=?,timing_locked=CASE WHEN ? THEN 1 ELSE timing_locked END,"
                "user_verified=CASE WHEN ? THEN 0 ELSE user_verified END,verified_fingerprint=CASE WHEN ? THEN NULL ELSE verified_fingerprint END,verified_at=CASE WHEN ? THEN NULL ELSE verified_at END,updated_at=CURRENT_TIMESTAMP WHERE clip_id=? AND lang=?",
                (offset, item["speedup"], item["trim_head_s"], item["trim_tail_s"], int(item["pause_tightening"]),
                 int(bool(update and update.manual)), int(moved >= 0.5), int(moved >= 0.5), int(moved >= 0.5), item["id"], lang),
            )
        after = _timing_state(connection, project_id, lang)
        _record_timing_history(connection, project_id, lang, before, after)
    try:
        _render_scheduled_track(project_id, lang, placements, video_end)
        source_value = project.get("preview_path") if project.get("preview_status") == "ready" else project.get("original_video_path") or project.get("video_path")
        if source_value:
            rebuilt = _rebuild_dubbed_preview(project_id, lang, Path(source_value), DATA_ROOT / "exports" / project_id)
            canonical = next(iter((DATA_ROOT / "exports" / project_id).glob(f"*_{lang}_phase3d_ai-dubbed.mp4")), None)
            if canonical:
                from backend.atomic_files import atomic_copyfile
                atomic_copyfile(rebuilt, canonical)
    except Exception as error:
        LOGGER.exception("Could not render timeline update for %s/%s", project_id, lang)
        raise HTTPException(500, f"Timeline saved, but mixed preview update failed: {str(error)[:200]}") from error
    return {"lang": lang, "placements": placements, "clips": await get_clips(project_id, lang)}


async def _restore_timeline_history(project_id: str, lang: str, direction: str) -> dict:
    project = project_row(project_id)
    with db.connect() as connection:
        predicate = "state='applied'" if direction == "undo" else "state='undone'"
        ordering = "DESC" if direction == "undo" else "ASC"
        record = connection.execute(
            f"SELECT id,before_json,after_json FROM timing_history WHERE project_id=? AND lang=? AND {predicate} ORDER BY id {ordering} LIMIT 1",
            (project_id, lang),
        ).fetchone()
        if record is None:
            raise HTTPException(409, f"Nothing to {direction}")
        state = json.loads(record["before_json"] if direction == "undo" else record["after_json"])
        current = _timing_state(connection, project_id, lang)
        for clip_id, values in state.items():
            old = current.get(clip_id, {})
            moved = abs(float(values.get("timing_offset_s") or 0) - float(old.get("timing_offset_s") or 0))
            connection.execute(
                "UPDATE dubs SET timing_offset_s=?,timing_speedup=?,trim_head_s=?,trim_tail_s=?,pause_tightening=?,timing_locked=?,"
                "user_verified=CASE WHEN ? THEN 0 ELSE user_verified END,verified_fingerprint=CASE WHEN ? THEN NULL ELSE verified_fingerprint END,"
                "verified_at=CASE WHEN ? THEN NULL ELSE verified_at END,updated_at=CURRENT_TIMESTAMP WHERE clip_id=? AND lang=?",
                (values.get("timing_offset_s", 0), values.get("timing_speedup", 1), values.get("trim_head_s", 0),
                 values.get("trim_tail_s", 0), values.get("pause_tightening", 0), values.get("timing_locked", 0),
                 int(moved >= .5), int(moved >= .5), int(moved >= .5), clip_id, lang),
            )
        connection.execute("UPDATE timing_history SET state=? WHERE id=?", ("undone" if direction == "undo" else "applied", record["id"]))
        rows = connection.execute(
            "SELECT c.id,c.start_s,c.end_s,d.duration_s,d.timing_offset_s,d.timing_speedup,d.trim_head_s,d.trim_tail_s,d.pause_tightening FROM clips c JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=? ORDER BY c.idx",
            (lang, project_id),
        ).fetchall()
    placements = []
    for row in rows:
        start = float(row["start_s"]) + float(row["timing_offset_s"] or 0)
        spoken = float(row["duration_s"] or 0)
        speed = float(row["timing_speedup"] or 1)
        head, tail = float(row["trim_head_s"] or 0), float(row["trim_tail_s"] or 0)
        placements.append({"id": row["id"], "start_s": start, "end_s": start + max(0, spoken-head-tail)/speed,
            "source_start_s": float(row["start_s"]), "source_end_s": float(row["end_s"]), "speech_duration_s": spoken,
            "speedup": speed, "trim_head_s": head, "trim_tail_s": tail, "pause_tightening": bool(row["pause_tightening"])})
    try:
        _render_scheduled_track(project_id, lang, placements, float(project["duration_s"] or 0))
        source_value = project.get("preview_path") if project.get("preview_status") == "ready" else project.get("original_video_path") or project.get("video_path")
        if source_value:
            rebuilt = _rebuild_dubbed_preview(project_id, lang, Path(source_value), DATA_ROOT / "exports" / project_id)
            canonical = next(iter((DATA_ROOT / "exports" / project_id).glob(f"*_{lang}_phase3d_ai-dubbed.mp4")), None)
            if canonical:
                from backend.atomic_files import atomic_copyfile
                atomic_copyfile(rebuilt, canonical)
    except Exception as error:
        raise HTTPException(500, f"Timeline restored, but preview rebuild failed: {str(error)[:200]}") from error
    return {"lang": lang, "direction": direction, "clips": await get_clips(project_id, lang)}


@app.post("/api/projects/{project_id}/timeline/{lang}/undo")
async def undo_timeline(project_id: str, lang: str) -> dict:
    return await _restore_timeline_history(project_id, lang, "undo")


@app.post("/api/projects/{project_id}/timeline/{lang}/redo")
async def redo_timeline(project_id: str, lang: str) -> dict:
    return await _restore_timeline_history(project_id, lang, "redo")


@app.post("/api/clips/{clip_id}/dubs/{lang}/trim")
async def trim_dub_take(clip_id: str, lang: str, body: TrimRequest) -> dict:
    with db.connect() as connection:
        row = connection.execute(
            "SELECT d.audio_path,d.duration_s,c.project_id FROM dubs d JOIN clips c ON c.id=d.clip_id WHERE c.id=? AND d.lang=?",
            (clip_id, lang),
        ).fetchone()
        if row is None or not row["audio_path"] or not Path(row["audio_path"]).is_file():
            raise HTTPException(404, "Saved speech take is unavailable")
        source = Path(row["audio_path"])
        target = DATA_ROOT / "exports" / row["project_id"] / "takes" / f"{clip_id}_{lang}_trimmed.wav"
    try:
        duration = _trim_take_audio(source, target, body.tighten_pauses)
        peaks = _audio_peaks(target)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        target.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not safely trim this take: {str(error)[:200]}") from error
    with db.connect() as connection:
        before = _timing_state(connection, row["project_id"], lang)
        connection.execute(
            "UPDATE dubs SET audio_path=?,duration_s=?,trim_head_s=0,trim_tail_s=0,pause_tightening=?,user_verified=0,verified_fingerprint=NULL,verified_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE clip_id=? AND lang=?",
            (str(target), duration, int(body.tighten_pauses), clip_id, lang),
        )
        after = _timing_state(connection, row["project_id"], lang)
        _record_timing_history(connection, row["project_id"], lang, before, after)
        project = connection.execute("SELECT duration_s,preview_status,preview_path,original_video_path,video_path FROM projects WHERE id=?", (row["project_id"],)).fetchone()
        timeline_rows = connection.execute(
            "SELECT c.id,c.start_s,c.end_s,d.duration_s,d.timing_offset_s,d.timing_speedup,d.trim_head_s,d.trim_tail_s,d.pause_tightening "
            "FROM clips c JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=? ORDER BY c.idx",
            (lang, row["project_id"]),
        ).fetchall()
    placements = []
    for item in timeline_rows:
        start_s = float(item["start_s"]) + float(item["timing_offset_s"] or 0)
        spoken_s = float(item["duration_s"] or 0)
        speed = max(1.0, float(item["timing_speedup"] or 1))
        trim_head = float(item["trim_head_s"] or 0)
        trim_tail = float(item["trim_tail_s"] or 0)
        placements.append({"id": item["id"], "start_s": start_s,
            "end_s": start_s + max(0, spoken_s-trim_head-trim_tail)/speed,
            "source_start_s": float(item["start_s"]), "source_end_s": float(item["end_s"]),
            "speech_duration_s": spoken_s, "speedup": speed, "trim_head_s": trim_head,
            "trim_tail_s": trim_tail, "pause_tightening": bool(item["pause_tightening"])})
    try:
        _render_scheduled_track(row["project_id"], lang, placements, float(project["duration_s"] or 0))
        source_value = project["preview_path"] if project["preview_status"] == "ready" else project["original_video_path"] or project["video_path"]
        if source_value:
            rebuilt = _rebuild_dubbed_preview(row["project_id"], lang, Path(source_value), DATA_ROOT / "exports" / row["project_id"])
            canonical = next(iter((DATA_ROOT / "exports" / row["project_id"]).glob(f"*_{lang}_phase3d_ai-dubbed.mp4")), None)
            if canonical:
                from backend.atomic_files import atomic_copyfile
                atomic_copyfile(rebuilt, canonical)
    except Exception as error:
        LOGGER.exception("Could not rebuild dub after trimming %s/%s", clip_id, lang)
        raise HTTPException(500, f"Take was trimmed, but the mixed preview could not be rebuilt: {str(error)[:200]}") from error
    return {"clip_id": clip_id, "lang": lang, "duration_s": duration, "waveform_peaks": peaks,
            "audio_url": f"/api/clips/{clip_id}/dubs/{lang}/audio", "pause_tightening": body.tighten_pauses}


@app.patch("/api/clips/{clip_id}/source")
async def edit_clip_source(clip_id: str, body: SourceEdit) -> dict:
    source_text = body.source_text.strip()
    if not source_text:
        raise HTTPException(422, "Source text cannot be empty")
    with db.connect() as connection:
        clip = connection.execute("SELECT project_id,idx FROM clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            raise HTTPException(404, "Clip not found")
        connection.execute("UPDATE clips SET src_text=? WHERE id=?", (source_text, clip_id))
        connection.execute(
            "UPDATE dubs SET status='yellow',reasons_json=?,updated_at=CURRENT_TIMESTAMP WHERE clip_id=?",
            (json.dumps(["Source text changed; re-speak this clip to update its translation."], ensure_ascii=False), clip_id),
        )
    output_dir = DATA_ROOT / "exports" / clip["project_id"]
    for report_path in output_dir.glob("*_phase3d_ai-dubbed.json"):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for report_clip in report.get("clips", []):
                if int(report_clip.get("clip", -1)) == int(clip["idx"]) + 1:
                    report_clip["source_cleaned"] = source_text
                    report_clip["source_raw"] = source_text
            atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        except (OSError, ValueError, TypeError):
            LOGGER.warning("Could not update source text in report %s", report_path)
    return {"source_text": source_text, "status": "updated"}


async def run_job(job_id: str) -> None:
    with db.connect() as connection:
        row = connection.execute("SELECT j.project_id,j.lang,j.kind,p.target_langs_json FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.id=?", (job_id,)).fetchone()
    if row is None:
        return
    languages = [row["lang"]] if row["kind"] in {"regenerate", "regenerate_clip", "retry_language"} and row["lang"] else json.loads(row["target_langs_json"])
    locks = [_project_language_locks.setdefault((row["project_id"], lang), asyncio.Lock()) for lang in sorted(languages)]
    if any(lock.locked() for lock in locks):
        LOGGER.info("Skipping duplicate active job %s for %s", job_id, row["project_id"])
        return
    for lock in locks:
        await lock.acquire()
    try:
        await _run_job_once(job_id)
    finally:
        for lock in reversed(locks):
            lock.release()


async def _run_job_once(job_id: str) -> None:
    with db.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return
        project = connection.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        voice = connection.execute("SELECT * FROM voices WHERE id=?", (job["voice_id"],)).fetchone() if job["voice_mode"] == "clone" else None
        checkpoint_state = json.loads(job["checkpoint_json"] or "{}")
        connection.execute("UPDATE jobs SET state='running',stage='resuming',error=NULL,attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    project = dict(project)
    if project.get("preview_status") == "pending":
        preview_task = _preview_tasks.get(project["id"])
        if preview_task:
            await preview_task
        with db.connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (project["id"],)).fetchone()
        project = dict(project)
    output_dir = DATA_ROOT / "exports" / project["id"]
    checkpoint_path = output_dir / f".job-{job_id}.checkpoint.json"
    if checkpoint_path.is_file():
        try:
            file_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if isinstance(file_checkpoint, dict):
                checkpoint_state.update(file_checkpoint)
        except (OSError, ValueError):
            LOGGER.warning("Ignoring unreadable job checkpoint %s", checkpoint_path)

    def check_continue() -> None:
        if _shutdown_requested.is_set():
            raise JobInterrupted("Service shutdown requested; job checkpointed for resume")

    def save_checkpoint(stage: str, state: dict) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(state, ensure_ascii=False)
        atomic_write_text(checkpoint_path, encoded)
        with db.connect() as connection:
            connection.execute(
                "UPDATE jobs SET checkpoint_json=?,stage=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (encoded, stage, job_id),
            )
            if stage == "clip":
                lang = state.get("current_language")
                index = state.get("last_clip_index")
                saved = state.get("clips", {}).get(lang, {}).get(str(index))
                if saved:
                    clip = dict(saved["clip"])
                    clip["audio_path"] = output_dir / saved["audio_key"]
                    clip_row = connection.execute(
                        "SELECT id FROM clips WHERE project_id=? AND idx=?", (project["id"], int(clip["clip"])-1),
                    ).fetchone()
                    clip_id = clip_row["id"] if clip_row else new_id()
                    if not clip_row:
                        connection.execute(
                            "INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                            (clip_id, project["id"], int(clip["clip"])-1, clip["start_s"], clip["end_s"], clip["source_cleaned"]),
                        )
                    persist_generated_dub(connection, clip_id, lang, clip)

    try:
        targeted_clip_index = None
        targeted_style_indices: dict[str, list[int]] = checkpoint_state.get("style_clip_indices", {})
        translation_override = None
        preserve_translation = False
        if job["kind"] == "regenerate_clip":
            with db.connect() as connection:
                clip = connection.execute("SELECT * FROM clips WHERE id=? AND project_id=?", (job["clip_id"], project["id"])).fetchone()
                dub = connection.execute("SELECT translation,human_edited FROM dubs WHERE clip_id=? AND lang=?", (job["clip_id"], job["lang"])).fetchone()
            if clip is None:
                raise ValueError("The selected source clip is no longer available for regeneration")
            targeted_clip_index = int(clip["idx"])
            # Missing translations are regenerated from the saved source clip below.
            translation_override = dub["translation"] if dub else None
            preserve_translation = bool(dub and dub["human_edited"])
        targets = ([job["lang"]] if job["kind"] in {"regenerate", "regenerate_clip", "retry_language"} and job["lang"] else json.loads(project["target_langs_json"]))
        completed = set()
        with db.connect() as connection:
            saved_clip_count = connection.execute("SELECT COUNT(*) FROM clips WHERE project_id=?", (project["id"],)).fetchone()[0]
            checkpoint_clip_count = len((checkpoint_state.get("term_fixes") or {}).get("cleaned_rows") or [])
            expected_clip_count = checkpoint_clip_count or saved_clip_count
            if expected_clip_count:
                complete_rows = connection.execute(
                    "SELECT d.lang,COUNT(*) AS total,SUM(CASE WHEN TRIM(d.translation)<>'' THEN 1 ELSE 0 END) AS translated "
                    "FROM dubs d JOIN clips c ON c.id=d.clip_id WHERE c.project_id=? GROUP BY d.lang",
                    (project["id"],),
                ).fetchall()
                completed = {row["lang"] for row in complete_rows
                             if int(row["total"] or 0) == expected_clip_count
                             and int(row["translated"] or 0) == expected_clip_count}
        output_dir = DATA_ROOT / "exports" / project["id"]
        embedding = json.loads(voice["embedding_json"]) if voice else None
        with db.connect() as connection:
            project_terms = [
                term_row_payload(row)
                for row in connection.execute("SELECT * FROM terms WHERE project_id=?", (project["id"],)).fetchall()
            ]
            global_terms = [
                term_row_payload(row)
                for row in connection.execute("SELECT * FROM global_terms").fetchall()
            ]
        seed_terms = project_terms + global_terms
        pending_targets = [
            lang for lang in targets
            if job["kind"] in {"regenerate", "regenerate_clip", "style_switch"}
            or lang not in completed
            or not list(output_dir.glob(f"*_{lang}*.json"))
        ]
        if pending_targets:
            with db.connect() as connection:
                connection.execute(
                    "UPDATE jobs SET stage='transcribing and preparing clips',done=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
            # Run languages independently: a provider failure in one language
            # must not discard a completed language. Transcript checkpoints are shared.
            results = []
            language_errors = checkpoint_state.setdefault("language_errors", {})
            for target_lang in pending_targets:
                checkpoint_state["current_language"] = target_lang
                try:
                    if job["kind"] == "retry_language":
                        # A failed language can have a sparse or stale clip checkpoint.
                        # Rebuild that language from the saved transcript and DB wording,
                        # while keeping the other languages and paid transcription cache.
                        with db.connect() as connection:
                            saved_rows = connection.execute(
                                "SELECT c.idx,d.translation FROM clips c JOIN dubs d ON d.clip_id=c.id "
                                "WHERE c.project_id=? AND d.lang=? ORDER BY c.idx",
                                (project["id"], target_lang),
                            ).fetchall()
                        prepare_language_retry_checkpoint(
                            checkpoint_state, target_lang,
                            {int(row["idx"]): row["translation"] for row in saved_rows},
                        )
                    style_indices = targeted_style_indices.get(target_lang) if job["kind"] == "style_switch" else None
                    clip_indices = style_indices if style_indices is not None else [targeted_clip_index]
                    for clip_index in clip_indices:
                        if job["kind"] == "style_switch":
                            with db.connect() as connection:
                                current = connection.execute(
                                    "SELECT d.human_edited,d.timing_locked,d.user_verified FROM clips c JOIN dubs d ON d.clip_id=c.id WHERE c.project_id=? AND c.idx=? AND d.lang=?",
                                    (project["id"], clip_index, target_lang),
                                ).fetchone()
                            if not current or not style_switch_eligible(current):
                                continue
                            checkpoint_state.get("translations", {}).get(target_lang, {}).pop(str(clip_index), None)
                            checkpoint_state.get("clips", {}).get(target_lang, {}).pop(str(clip_index), None)
                        language_results = await asyncio.to_thread(
                            run_quality_pass,
                            Path(project["video_path"]), Path(voice["reference_path"]) if voice else None, [target_lang],
                            output_dir, settings, job["content_type"], job["translation_style"], embedding,
                            project["source_lang"], seed_terms, job["stock_voice"] if job["voice_mode"] == "stock" else None,
                            only_clip_index=clip_index, translation_override=translation_override,
                            preserve_translation=preserve_translation, resume_state=checkpoint_state,
                            checkpoint_callback=save_checkpoint, continue_check=check_continue,
                        )
                        results.extend(language_results)
                    language_errors.pop(target_lang, None)
                except JobInterrupted:
                    raise
                except Exception as exc:
                    language_errors[target_lang] = str(exc)[:500]
                    checkpoint_state["current_language"] = target_lang
                    save_checkpoint("language_failed", checkpoint_state)
                    LOGGER.exception("Language %s failed for project %s", target_lang, project["id"])
            for result_index, result in enumerate(results):
                lang = result["language"]
                with db.connect() as connection:
                    connection.execute(
                        "UPDATE jobs SET stage=?,done=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (f"saving {lang}", result_index, job_id),
                    )
                    for term in result.get("term_sheet", {}).get("terms", []):
                        exists = connection.execute("SELECT id FROM terms WHERE project_id=? AND term=?", (project["id"], term["term"])).fetchone()
                        if not exists:
                            connection.execute(
                                "INSERT INTO terms(id,project_id,term,type,treatment,locked,targets_json,source,rule_type,heard_variants_json,spoken_forms_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (new_id(), project["id"], term["term"], term["type"], term["treatment"], int(term["locked"]), json.dumps(term["targets"], ensure_ascii=False), "brain", term.get("rule_type", "protected"), json.dumps(term.get("heard_variants", []), ensure_ascii=False), json.dumps(term.get("spoken_forms", {}), ensure_ascii=False)),
                            )
                    for clip in result["clips"]:
                        if job["kind"] == "style_switch" and int(clip["clip"]) - 1 not in targeted_style_indices.get(lang, []):
                            continue
                        row = connection.execute("SELECT id FROM clips WHERE project_id=? AND idx=?", (project["id"], clip["clip"]-1)).fetchone()
                        clip_id = row["id"] if row else new_id()
                        if not row:
                            connection.execute("INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)", (clip_id, project["id"], clip["clip"]-1, clip["start_s"], clip["end_s"], clip["source_cleaned"]))
                        persist_generated_dub(connection, clip_id, lang, clip)
        with db.connect() as connection:
            clip_count = connection.execute("SELECT COUNT(*) FROM clips WHERE project_id=?", (project["id"],)).fetchone()[0]
            if clip_count == 0:
                errors = checkpoint_state.get("language_errors") or {}
                detail = next(iter(errors.values()), "No transcript was created.")
                raise ValueError(f"Processing created no clips. {detail}")
            source_count = len((checkpoint_state.get("term_fixes") or {}).get("cleaned_rows") or [])
            selected_languages = json.loads(project["target_langs_json"])
            for target_lang in selected_languages:
                rows = connection.execute(
                    "SELECT c.idx,d.translation FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id AND d.lang=? "
                    "WHERE c.project_id=? ORDER BY c.idx", (target_lang, project["id"]),
                ).fetchall()
                expected = source_count or clip_count
                missing = [row["idx"] + 1 for row in rows if not str(row["translation"] or "").strip()]
                if len(rows) != expected or missing:
                    failed_clip = missing[0] if missing else min(len(rows) + 1, expected)
                    detail = checkpoint_state.get("language_errors", {}).get(target_lang, "translation is missing")
                    language_name = LANGUAGES.get(target_lang, target_lang)
                    raise ValueError(f"{language_name} failed on clip {failed_clip}: {detail}")
            connection.execute("UPDATE jobs SET state='complete',stage='complete',done=total,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            connection.execute("UPDATE projects SET status='complete' WHERE id=?", (project["id"],))
    except JobInterrupted as error:
        LOGGER.info("Dub job %s interrupted safely for restart", job_id)
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='interrupted',stage='interrupted',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(error)[:1000], job_id))
            connection.execute("UPDATE projects SET status='interrupted' WHERE id=?", (project["id"],))
    except Exception as error:
        LOGGER.exception("Dub job %s failed", job_id)
        reason = getattr(error, "message", "Server storage is full") if is_storage_full(error) else str(error)[:1000]
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='failed',stage='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (reason, job_id))
            connection.execute("UPDATE projects SET status='failed' WHERE id=?", (project["id"],))


@app.post("/api/projects/{project_id}/start", status_code=202)
async def start_job(request: Request, project_id: str, body: StartJob, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(409, "Upload a video first")
    if body.voice_mode not in {"clone", "stock"}:
        raise HTTPException(422, "Choose clone or stock voice mode")
    if body.translation_style not in {"natural", "pure"}:
        raise HTTPException(422, "Translation style must be natural or pure")
    if body.voice_mode == "stock" and body.stock_voice not in STOCK_VOICES:
        raise HTTPException(422, "Choose an available Timbre stock voice")
    with db.connect() as connection:
        active = connection.execute(
            "SELECT id,state FROM jobs WHERE project_id=? AND state IN ('pending','running') ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            return {"job_id": active["id"], "state": active["state"], "already_running": True}
        voice = (connection.execute("SELECT id FROM voices WHERE id=? AND consent_ok=1", (body.voice_id,)).fetchone()
                 if body.voice_mode == "clone" and body.voice_id else None)
        if body.voice_mode == "clone" and not voice:
            raise HTTPException(409, "Choose a voice with a matched live consent recording")
        credits = estimated_job_credits(project["duration_s"], len(json.loads(project["target_langs_json"])))
        if settings.max_credits_per_job and credits > settings.max_credits_per_job:
            raise HTTPException(429, f"Estimated job cost {credits} exceeds the per-job safety cap")
        used_today = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
        if used_today + credits > settings.gnani_daily_limit:
            raise HTTPException(429, "Daily Gnani safety cap reached; try again tomorrow")
        job_id = new_id()
        connection.execute("INSERT INTO jobs(id,project_id,kind,voice_id,content_type,translation_style,voice_mode,stock_voice,state,stage,total) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, project_id, "dub", body.voice_id if body.voice_mode == "clone" else None, body.content_type, body.translation_style, body.voice_mode, body.stock_voice if body.voice_mode == "stock" else None, "pending", "queued", len(json.loads(project["target_langs_json"]))))
        connection.execute("UPDATE projects SET status='processing' WHERE id=?", (project_id,))
        connection.execute("UPDATE projects SET translation_style=? WHERE id=?", (body.translation_style, project_id))
        connection.execute("INSERT INTO credit_ledger(id,project_id,kind,units,est_credits) VALUES(?,?,?,?,?)", (new_id(), project_id, "dub-job-estimate", 1, credits))
    background.add_task(schedule_job, job_id)
    return {"job_id": job_id, "state": "pending"}


@app.post("/api/projects/{project_id}/resume", status_code=202)
async def resume_job(request: Request, project_id: str, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    project_row(project_id)
    with db.connect() as connection:
        job = connection.execute(
            "SELECT j.id,j.state FROM jobs j WHERE j.project_id=? AND (j.state IN ('interrupted','failed') "
            "OR (j.state='complete' AND (SELECT COUNT(*) FROM clips WHERE project_id=j.project_id)=0)) "
            "ORDER BY j.created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(409, "There is no interrupted or failed job to resume")
        connection.execute("UPDATE jobs SET state='pending',stage='queued',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
        connection.execute("UPDATE projects SET status='processing' WHERE id=?", (project_id,))
    background.add_task(schedule_job, job["id"])
    return {"job_id": job["id"], "state": "pending"}


@app.post("/api/projects/{project_id}/retry-language/{lang}", status_code=202)
async def retry_language(request: Request, project_id: str, lang: str, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    project = project_row(project_id)
    if lang not in json.loads(project["target_langs_json"]):
        raise HTTPException(422, "Language is not a target of this project")
    with db.connect() as connection:
        active = connection.execute(
            "SELECT id,state,lang FROM jobs WHERE project_id=? AND state IN ('pending','running') ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            return {"job_id": active["id"], "state": active["state"], "lang": active["lang"], "already_running": True}
        job = connection.execute("SELECT id,state FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
        if job is None or job["state"] == "running":
            raise HTTPException(409, "No retryable job is available for this language")
        connection.execute("UPDATE jobs SET kind='retry_language',lang=?,state='pending',stage='queued',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (lang, job["id"]))
        connection.execute("UPDATE projects SET status='processing' WHERE id=?", (project_id,))
    background.add_task(schedule_job, job["id"])
    return {"job_id": job["id"], "state": "pending", "lang": lang}


@app.get("/api/projects/{project_id}/events")
async def project_events(project_id: str):
    project_row(project_id)

    async def events():
        last = None
        while True:
            with db.connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
            if row:
                payload = {"project_id": project_id, "lang": row["lang"], "stage": row["stage"], "done": row["done"], "total": row["total"], "message": row["error"] or row["stage"], "kind": row["kind"], "clip_id": row["clip_id"], "state": row["state"]}
                serialized = json.dumps(payload)
                if serialized != last:
                    yield f"data: {serialized}\n\n"
                    last = serialized
                if row["state"] in {"complete", "failed", "interrupted"}:
                    break
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/clips/{clip_id}/dubs/{lang}/regenerate", status_code=202)
async def regenerate_clip(request: Request, clip_id: str, lang: str, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    with db.connect() as connection:
        clip = connection.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            raise HTTPException(404, "Clip not found")
        project = connection.execute("SELECT * FROM projects WHERE id=?", (clip["project_id"],)).fetchone()
        if not project or not project["video_path"]:
            raise HTTPException(409, "Project source video is unavailable")
        if lang not in json.loads(project["target_langs_json"]):
            raise HTTPException(422, "Language is not a target of this project")
        voice = connection.execute(
            "SELECT j.voice_id,j.content_type,j.translation_style,j.voice_mode,j.stock_voice FROM jobs j "
            "LEFT JOIN voices v ON v.id=j.voice_id "
            "WHERE j.project_id=? AND ((j.voice_mode='clone' AND j.voice_id IS NOT NULL AND v.consent_ok=1) "
            "OR (j.voice_mode='stock' AND j.stock_voice IN ('Avinash','Shivani','Lakshmi'))) ORDER BY j.rowid DESC LIMIT 1",
            (clip["project_id"],),
        ).fetchone()
        if voice is None:
            raise HTTPException(409, "No project voice choice is available for regeneration")
        credits = estimated_job_credits(float(clip["end_s"] - clip["start_s"]), 1)
        if settings.max_credits_per_job and credits > settings.max_credits_per_job:
            raise HTTPException(429, f"Estimated regeneration cost {credits} exceeds the per-job safety cap")
        used_today = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
        if used_today + credits > settings.gnani_daily_limit:
            raise HTTPException(429, "Daily Gnani safety cap reached; try again tomorrow")
        job_id = new_id()
        connection.execute(
            "INSERT INTO jobs(id,project_id,kind,lang,clip_id,voice_id,content_type,translation_style,voice_mode,stock_voice,state,stage,total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (job_id, clip["project_id"], "regenerate_clip", lang, clip_id, voice["voice_id"], voice["content_type"], voice["translation_style"], voice["voice_mode"], voice["stock_voice"], "pending", "queued"),
        )
        connection.execute("UPDATE projects SET status='processing' WHERE id=?", (clip["project_id"],))
        connection.execute("INSERT INTO credit_ledger(id,project_id,kind,units,est_credits) VALUES(?,?,?,?,?)",
                           (new_id(), clip["project_id"], "regenerate-language-estimate", 1, credits))
    background.add_task(schedule_job, job_id)
    return {"queued": True, "job_id": job_id, "state": "pending", "language": lang,
            "clip_id": clip_id, "scope": "selected clip only", "estimated_credits": credits}


@app.post("/api/clips/{clip_id}/dubs/{lang}/revert")
async def revert_dub(clip_id: str, lang: str) -> dict:
    with db.connect() as connection:
        dub = connection.execute("SELECT * FROM dubs WHERE clip_id=? AND lang=?", (clip_id, lang)).fetchone()
        if not dub:
            raise HTTPException(404, "Dub not found")
        connection.execute("UPDATE dubs SET translation=draft_translation,human_edited=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (dub["id"],))
    return {"translation": dub["draft_translation"], "status": dub["status"]}


_EXPORT_FORMATS = {"mp4": ".mp4", "srt": ".srt", "report": ".json", "audio_wav": ".wav", "audio_mp3": ".mp3"}
_REQUESTED_EXPORTS = {"mp4": "mp4", "srt": "srt", "report": "report", "audio": "audio_wav"}


def _language_artifacts(project_id: str, lang: str) -> dict[str, Path]:
    folder = DATA_ROOT / "exports" / project_id
    if not folder.is_dir():
        return {}
    candidates: dict[str, list[Path]] = {key: [] for key in _EXPORT_FORMATS}
    for path in folder.iterdir():
        if not path.is_file() or f"_{lang}" not in path.name:
            continue
        try:
            if not path.resolve().is_relative_to(folder.resolve()):
                continue
        except OSError:
            continue
        for artifact, suffix in _EXPORT_FORMATS.items():
            if path.suffix.lower() == suffix and (artifact != "mp4" or "ai-dubbed" in path.stem):
                candidates[artifact].append(path)
    return {key: max(paths, key=lambda item: item.stat().st_mtime) for key, paths in candidates.items() if paths}


def _export_preflight(project_id: str, lang: str, formats: list[str]) -> dict:
    project = project_row(project_id)
    targets = json.loads(project["target_langs_json"])
    if lang not in targets:
        raise HTTPException(422, "Language is not a target of this project")
    unknown = sorted(set(formats) - set(_REQUESTED_EXPORTS))
    if unknown:
        raise HTTPException(422, f"Unsupported export format(s): {', '.join(unknown)}")
    with db.connect() as connection:
        clip_count = connection.execute("SELECT COUNT(*) FROM clips WHERE project_id=?", (project_id,)).fetchone()[0]
        dubs = connection.execute(
            "SELECT c.idx,d.status,d.audio_stale,d.user_verified FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id AND d.lang=? "
            "WHERE c.project_id=? ORDER BY c.idx", (lang, project_id)
        ).fetchall()
    unresolved_red = [int(row["idx"]) + 1 for row in dubs if row["status"] == "red" and not row["user_verified"]]
    artifacts = _language_artifacts(project_id, lang)
    checks: list[dict] = []
    resolved: dict[str, Path] = {}
    requested_artifacts = [_REQUESTED_EXPORTS[artifact] for artifact in formats]
    for artifact in requested_artifacts:
        path = artifacts.get(artifact)
        if not path:
            display_name = "audio (WAV)" if artifact == "audio_wav" else artifact
            checks.append({"name": f"{artifact}_available", "passed": False, "detail": f"No {display_name} export exists for {lang}"})
        else:
            resolved[artifact] = path
            checks.append({"name": f"{artifact}_available", "passed": True, "detail": path.name})

    mp4 = artifacts.get("mp4")
    if mp4 and ("mp4" in formats or "srt" in formats or "audio" in formats):
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(mp4)],
                check=True, capture_output=True, text=True,
            )
            metadata = json.loads(probe.stdout)
            streams = metadata.get("streams", [])
            video_count = sum(stream.get("codec_type") == "video" for stream in streams)
            audio_count = sum(stream.get("codec_type") == "audio" for stream in streams)
            checks.append({"name": "mp4_streams", "passed": video_count == 1 and audio_count == 1,
                           "detail": f"{video_count} video stream(s), {audio_count} audio stream(s); expected one each"})
            actual_duration = float(metadata.get("format", {}).get("duration", 0) or 0)
            expected_duration = float(project["duration_s"] or 0)
            close_duration = expected_duration > 0 and abs(actual_duration - expected_duration) <= 0.1
            checks.append({"name": "duration", "passed": close_duration,
                           "detail": f"output {actual_duration:.3f}s; source {expected_duration:.3f}s; tolerance 0.1s"})
            volume = subprocess.run(
                ["ffmpeg", "-i", str(mp4), "-vn", "-af", "volumedetect", "-f", "null", "NUL" if os.name == "nt" else "/dev/null"],
                check=False, capture_output=True, text=True,
            )
            match = re.search(r"mean_volume:\s*(-?inf|-?[\d.]+) dB", volume.stderr, re.IGNORECASE)
            mean_db = None if not match or match.group(1).lower() == "-inf" else float(match.group(1))
            checks.append({"name": "audio_not_silent", "passed": mean_db is not None and mean_db > -55,
                           "detail": f"mean volume {mean_db:.1f} dB" if mean_db is not None else "audio mean volume could not be measured"})
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
            checks.append({"name": "mp4_self_check", "passed": False, "detail": f"ffprobe/ffmpeg check failed: {error}"})

    srt = artifacts.get("srt")
    if srt and "srt" in formats:
        try:
            text = srt.read_text(encoding="utf-8-sig")
            cue_count = len(re.findall(r"(?m)^\s*\d+\s*$", text))
            checks.append({"name": "srt_cue_count", "passed": cue_count == clip_count,
                           "detail": f"{cue_count} cue(s); {clip_count} clip(s) in database"})
        except OSError as error:
            checks.append({"name": "srt_readable", "passed": False, "detail": str(error)})
    report = artifacts.get("report")
    if report and "report" in formats:
        try:
            report_data = json.loads(report.read_text(encoding="utf-8"))
            report_clips = report_data.get("clips")
            checks.append({"name": "report_readable", "passed": isinstance(report_clips, list),
                           "detail": f"JSON report with {len(report_clips)} clip record(s)" if isinstance(report_clips, list) else "JSON report has no clips array"})
        except (OSError, json.JSONDecodeError) as error:
            checks.append({"name": "report_readable", "passed": False, "detail": str(error)})

    stale_clips = [int(row["idx"]) + 1 for row in dubs if row["audio_stale"]]
    stale = bool(stale_clips)
    checks.append({"name": "not_stale", "passed": not stale,
                   "detail": (f"Saved translation has no matching generated audio for clip(s): {', '.join(map(str, stale_clips))}. Re-speak those clips before exporting."
                              if stale else "All saved translations have current generated audio")})
    checks.append({"name": "red_clips", "passed": not unresolved_red,
                   "detail": "No unresolved red clips" if not unresolved_red else f"Unresolved red clips: {', '.join(map(str, unresolved_red))}"})
    missing_dub_rows = [int(row["idx"]) + 1 for row in dubs if row["status"] is None]
    if missing_dub_rows:
        checks.append({"name": "all_clips_have_dub", "passed": False,
                       "detail": f"No saved dub for clip(s): {', '.join(map(str, missing_dub_rows))}"})
    return {
        "project_id": project_id, "lang": lang, "requested_formats": formats,
        "available_formats": sorted(("audio" if key == "audio_wav" else "audio_mp3" if key == "audio_mp3" else key) for key in artifacts), "clip_count": int(clip_count),
        "unresolved_red_clips": unresolved_red, "stale": stale,
        "checks": checks, "passed": all(check["passed"] for check in checks),
        "downloads": {("audio" if key == "audio_wav" else "audio-mp3" if key == "audio_mp3" else key): f"/api/projects/{project_id}/downloads/{('audio' if key == 'audio_wav' else 'audio-mp3' if key == 'audio_mp3' else key)}?lang={lang}" for key in resolved},
        "label": "AI dubbed",
    }


def _export_history(project_id: str, lang: str) -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT id,formats_json,unresolved_red_json,created_at FROM export_events "
            "WHERE project_id=? AND lang=? ORDER BY created_at DESC,id DESC",
            (project_id, lang),
        ).fetchall()
    return [{"id": row["id"], "formats": json.loads(row["formats_json"]),
             "unresolved_red_clips": json.loads(row["unresolved_red_json"]),
             "exported_with_unresolved_red": bool(json.loads(row["unresolved_red_json"])),
             "created_at": row["created_at"]} for row in rows]


@app.post("/api/projects/{project_id}/export/preflight")
async def export_preflight(project_id: str, body: ExportRequest) -> dict:
    return _export_preflight(project_id, body.lang, body.formats)


@app.post("/api/projects/{project_id}/export")
async def export_project(request: Request, project_id: str, body: ExportRequest) -> dict:
    enforce_ip_limit(request, "previews")
    project = project_row(project_id)
    unknown = sorted(set(body.formats) - set(_REQUESTED_EXPORTS))
    if unknown:
        raise HTTPException(422, f"Unsupported export format(s): {', '.join(unknown)}")
    if body.lang not in json.loads(project["target_langs_json"]):
        raise HTTPException(422, "Language is not a target of this project")
    if not body.allow_unresolved_red:
        with db.connect() as connection:
            red = connection.execute(
                "SELECT c.idx FROM clips c JOIN dubs d ON d.clip_id=c.id "
                "WHERE c.project_id=? AND d.lang=? AND d.status='red' AND COALESCE(d.user_verified,0)=0 ORDER BY c.idx",
                (project_id, body.lang),
            ).fetchall()
        if red:
            raise HTTPException(409, detail={"message": "Confirm export with unresolved red clips", "unresolved_red_clips": [int(row["idx"])+1 for row in red]})
    if "audio" in body.formats:
        project_row(project_id)
        mp4 = _language_artifacts(project_id, body.lang).get("mp4")
        if not mp4:
            raise HTTPException(409, "Audio-only export needs an existing dubbed MP4")
        wav_path, mp3_path = mp4.with_suffix(".wav"), mp4.with_suffix(".mp3")
        try:
            run_atomic_command(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav_path)], check=True, capture_output=True)
            run_atomic_command(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_path)], check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError) as error:
            if is_storage_full(error):
                raise StorageFullError() from error
            raise HTTPException(500, f"Audio-only export failed: {error}") from error
    result = _export_preflight(project_id, body.lang, body.formats)
    blockers = [check for check in result["checks"] if not check["passed"] and check["name"] != "red_clips"]
    if blockers:
        raise HTTPException(409, detail={"message": "Export preflight failed", "preflight": result})
    if result["unresolved_red_clips"] and not body.allow_unresolved_red:
        raise HTTPException(409, detail={"message": "Confirm export with unresolved red clips", "preflight": result})
    result["exported_with_unresolved_red"] = bool(result["unresolved_red_clips"])
    result["exported"] = True
    artifacts = _language_artifacts(project_id, body.lang)
    for key, artifact in (("audio", "audio"), ("audio-mp3", "audio-mp3")):
        if artifacts.get("audio_wav" if key == "audio" else "audio_mp3"):
            result["downloads"][key] = f"/api/projects/{project_id}/downloads/{artifact}?lang={body.lang}"
    result["files"] = {
        (".wav" if key == "audio_wav" else ".mp3" if key == "audio_mp3" else ".json" if key == "report" else f".{key}"): str(path)
        for key, path in artifacts.items() if key in {"mp4", "srt", "report", "audio_wav", "audio_mp3"}
    }
    export_id = new_id()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO export_events(id,project_id,lang,formats_json,unresolved_red_json) VALUES(?,?,?,?,?)",
            (export_id, project_id, body.lang, json.dumps(body.formats), json.dumps(result["unresolved_red_clips"])),
        )
        export_time = connection.execute("SELECT created_at FROM export_events WHERE id=?", (export_id,)).fetchone()[0]
    result["export_record"] = {"id": export_id, "created_at": export_time,
                               "formats": body.formats,
                               "unresolved_red_clips": result["unresolved_red_clips"],
                               "exported_with_unresolved_red": bool(result["unresolved_red_clips"])}
    cleanup_project_intermediates(project_id)
    return result


@app.get("/api/reports")
async def list_reports(project_id: str | None = None, lang: str | None = None) -> dict:
    with db.connect() as connection:
        projects = connection.execute("SELECT id,name FROM projects" + (" WHERE id=?" if project_id else ""), ([project_id] if project_id else [])).fetchall()
    reports = []
    for project in projects:
        folder = DATA_ROOT / "exports" / project["id"]
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            try:
                if not path.resolve().is_relative_to(folder.resolve()):
                    continue
            except OSError:
                continue
            match = re.search(r"_([a-z]{2}-IN)(?:_|\.)", path.name)
            report_lang = match.group(1) if match else None
            if lang and report_lang != lang:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            history = _export_history(project["id"], report_lang) if report_lang else []
            from datetime import datetime, timezone
            reports.append({
                "project_id": project["id"], "project_name": project["name"],
                "lang": payload.get("language") or report_lang,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "clip_count": len(payload.get("clips", [])) if isinstance(payload.get("clips"), list) else 0,
                "verification": payload.get("verification"), "file": path.name,
                "export_history": history,
                "exported_with_unresolved_red": any(item["exported_with_unresolved_red"] for item in history),
                "json_url": f"/api/projects/{project['id']}/downloads/report?lang={report_lang}" if report_lang else None,
                "page_url": f"/api/projects/{project['id']}/report/page?lang={report_lang}" if report_lang else None,
            })
    reports.sort(key=lambda item: item["created_at"], reverse=True)
    return {"reports": reports, "count": len(reports)}


def _read_project_report(project_id: str, lang: str) -> tuple[Path, dict]:
    project_row(project_id)
    if lang not in LANGUAGES:
        raise HTTPException(422, "Unsupported language")
    reports = _language_artifacts(project_id, lang)
    path = reports.get("report")
    if not path:
        raise HTTPException(404, "Verification report not found")
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(500, f"Verification report could not be read: {error}") from error


@app.get("/api/projects/{project_id}/report")
async def project_report(project_id: str, lang: str):
    _, report = _read_project_report(project_id, lang)
    report["export_history"] = _export_history(project_id, lang)
    return report


@app.get("/api/projects/{project_id}/report/page", response_class=HTMLResponse)
async def readable_project_report(project_id: str, lang: str):
    path, report = _read_project_report(project_id, lang)
    project = project_row(project_id)
    rows = report.get("clips", []) if isinstance(report.get("clips"), list) else []
    table_rows = []
    for row in rows:
        table_rows.append("<tr>" + "".join(
            f"<td>{html.escape(str(value if value is not None else ''))}</td>"
            for value in (row.get("clip", ""), row.get("start_s", ""), row.get("end_s", ""), row.get("status", ""), row.get("source_cleaned", row.get("source", "")), row.get("translation", ""), "; ".join(map(str, row.get("reasons", []))))
        ) + "</tr>")
    title = html.escape(f"{project['name']} · {lang} verification")
    verification_note = html.escape(str(report.get("verification_note") or ""))
    export_history = _export_history(project_id, lang)
    export_rows = "".join(
        f"<li>{html.escape(item['created_at'])} · formats: {html.escape(', '.join(item['formats']))}"
        f" · {'exported with unresolved red clips ' + html.escape(', '.join(map(str, item['unresolved_red_clips']))) if item['exported_with_unresolved_red'] else 'no unresolved red clips'}</li>"
        for item in export_history
    )
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title>
    <style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 20px;color:#171310}}h1{{font-size:2rem}}.meta{{color:#615a52}}table{{border-collapse:collapse;width:100%;margin-top:24px}}th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #ddd}}th{{background:#f4f0e8}}td{{max-width:360px;overflow-wrap:anywhere}}</style>
    <h1>{title}</h1><p class="meta">Verification: {html.escape(str(report.get('verification') or 'not recorded'))} · {len(rows)} clip(s) · Source report: {html.escape(path.name)}</p>
    {f'<p>{verification_note}</p>' if verification_note else ''}
    <h2>Export history</h2><ul>{export_rows or '<li>No successful export recorded.</li>'}</ul>
    <table><thead><tr><th>Clip</th><th>Start</th><th>End</th><th>Status</th><th>Source</th><th>Translation</th><th>Notes</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></html>"""
    return HTMLResponse(document)


def _track_artifact_path(project_id: str, lang: str, kind: str) -> Path | None:
    folder = (DATA_ROOT / "exports" / project_id).resolve()
    if not folder.is_dir():
        return None
    matches = list(folder.glob(f"*_{lang}_phase3d_ai-dubbed_{kind}-track.wav"))
    if not matches:
        return None
    path = matches[0].resolve()
    return path if path.is_file() and path.is_relative_to(folder) else None


def _validate_media_file(path: Path) -> None:
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("media file is empty")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    if not any(item.get("codec_type") == "video" for item in json.loads(probe.stdout).get("streams", [])):
        raise ValueError("media file has no video stream")


def _log_media_failure(path: Path, error: Exception) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        size = "missing"
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    LOGGER.warning("Media candidate failed path=%s size=%s ffprobe_stderr=%s error=%s",
                   path, size, (stderr or "")[:1000], error)


def _rebuild_dubbed_preview(project_id: str, lang: str, source: Path, output_dir: Path) -> Path:
    dubbed_track = _track_artifact_path(project_id, lang, "dubbed")
    if dubbed_track is None or not source.is_file():
        raise FileNotFoundError("Saved dubbed track or original upload is missing")
    playable_source = source
    duration, has_video = probe_video(playable_source)
    if not has_video:
        raise ValueError("Original upload has no video stream")
    output_dir.mkdir(parents=True, exist_ok=True)
    rebuilt = output_dir / f"{source.stem}_{lang}_phase3d_ai-dubbed.mp4"
    temporary = output_dir / f".{source.stem}_{lang}_phase3d_ai-dubbed.part.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(playable_source), "-i", str(dubbed_track),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-t", str(duration),
             "-movflags", "+faststart", str(temporary)],
            check=True, capture_output=True, timeout=240,
        )
        _validate_media_file(temporary)
        with temporary.open("r+b") as recovered_file:
            os.fsync(recovered_file.fileno())
        temporary.replace(rebuilt)
    finally:
        temporary.unlink(missing_ok=True)
    return rebuilt


@app.get("/api/projects/{project_id}/downloads/{artifact}")
async def download_project_artifact(project_id: str, artifact: str, lang: str):
    track_names = {"track-dubbed": "dubbed", "track-source": "source", "track-background": "background"}
    if artifact in track_names:
        project_row(project_id)
        path = _track_artifact_path(project_id, lang, track_names[artifact])
        if not path:
            raise HTTPException(404, f"{track_names[artifact].title()} track is not available for {lang}")
        return FileResponse(path, media_type="audio/wav", filename=path.name)
    download_keys = {"mp4": "mp4", "srt": "srt", "report": "report", "audio": "audio_wav", "audio-mp3": "audio_mp3"}
    if artifact not in download_keys:
        raise HTTPException(404, "Artifact format is not supported")
    project = project_row(project_id)
    path = _language_artifacts(project_id, lang).get(download_keys[artifact])
    if artifact == "mp4":
        mp4_usable = False
        if path is not None and path.is_file():
            try:
                _validate_media_file(path)
                mp4_usable = True
            except Exception as error:
                LOGGER.warning("Dubbed download is invalid; rebuilding project=%s lang=%s path=%s error=%s",
                               project_id, lang, path, str(error)[:300])
        if not mp4_usable:
            source_value = (
                project.get("preview_path") if project.get("preview_status") == "ready"
                else project.get("original_video_path") or project.get("video_path")
            )
            if source_value:
                try:
                    path = _rebuild_dubbed_preview(
                        project_id, lang, Path(source_value), DATA_ROOT / "exports" / project_id
                    )
                    LOGGER.info("Rebuilt missing or invalid dubbed download project=%s lang=%s path=%s", project_id, lang, path)
                except Exception:
                    LOGGER.exception("Could not rebuild missing or invalid dubbed download project=%s lang=%s", project_id, lang)
    if not path:
        raise HTTPException(404, f"{artifact} download not found for {lang}")
    media_types = {"mp4": "video/mp4", "srt": "application/x-subrip", "report": "application/json", "audio": "audio/wav", "audio-mp3": "audio/mpeg"}
    if artifact == "report":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["export_history"] = _export_history(project_id, lang)
        return JSONResponse(content=payload, headers={"Content-Disposition": 'attachment; filename="verification-report.json"'})
    return FileResponse(path, media_type=media_types[artifact], filename=path.name)


@app.get("/api/projects/{project_id}/tracks")
async def project_audio_tracks(project_id: str, lang: str) -> dict:
    project_row(project_id)
    tracks = {}
    for kind in ("dubbed", "source", "background"):
        artifact = f"track-{kind}"
        path = _track_artifact_path(project_id, lang, kind)
        tracks[kind] = f"/api/projects/{project_id}/downloads/{artifact}?lang={lang}" if path else None
    expected_tracks = set()
    report_path = _language_artifacts(project_id, lang).get("report")
    if report_path:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            expected_tracks = set((report.get("audio_tracks") or {}).keys())
        except (OSError, ValueError, TypeError):
            pass
    audio_removed = any(kind in expected_tracks and not tracks.get(kind) for kind in tracks)
    return {"language": lang, "tracks": tracks, "audio_removed": audio_removed,
            "mix_policy": "Dubbed and quiet room tone are mixed into the MP4; source audio is available separately and is not mixed under the dub."}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request) -> dict:
    _require_privacy_delete_password(request)
    _delete_project_data(project_id)
    return {"deleted": True}


@app.delete("/api/data")
async def delete_all_user_data(request: Request) -> dict:
    """Remove local uploads, generated exports, voice references, cached responses and database rows."""
    _require_privacy_delete_password(request)
    for folder in ("uploads", "exports", "voices"):
        shutil.rmtree(DATA_ROOT / folder, ignore_errors=True)
    with db.connect() as connection:
        for table in ("dubs", "clips", "terms", "global_terms", "export_events", "jobs", "credit_ledger", "projects", "voices"):
            connection.execute(f"DELETE FROM {table}")
    consent_sessions.clear()
    _rate_events.clear()
    return {"deleted": True}


@app.get("/studio", include_in_schema=False)
@app.get("/studio/{spa_path:path}", include_in_schema=False)
async def studio_spa_route(spa_path: str = ""):
    """Serve the React app on direct loads and refreshes of studio routes."""
    index = Path("backend/static/index.html")
    if not index.is_file():
        raise HTTPException(503, "Frontend build is not available")
    return FileResponse(index, media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000)


if Path("backend/static/index.html").exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory="backend/static", html=True), name="studio")
