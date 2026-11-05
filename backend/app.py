"""Anek HTTP API and static frontend host."""

import asyncio
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
import time
import uuid
from collections import defaultdict, deque
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
from backend.brain.checks import run_checks
from backend.clients.gnani import create_gnani_client
from backend.clients.llm import clear_response_cache
from backend.brain.termsheet import TermSheet
from backend.config import LANGUAGES, get_settings
from backend.pipeline.phase3b import run_quality_pass

settings = get_settings()
LOGGER = logging.getLogger(__name__)
app = FastAPI(title=settings.app_name)
DATA_ROOT = Path(settings.anek_data_dir)
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
        allow_headers=["Authorization", "Content-Type"],
    )


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


class StartJob(BaseModel):
    voice_id: str | None = None
    voice_mode: str = "clone"
    stock_voice: str | None = None
    content_type: str = "explainer"
    translation_style: str = "neutral"


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
    connection.execute(
        "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,reasons_json,attempts_json,audio_path,duration_s) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(clip_id,lang) DO UPDATE SET translation=CASE WHEN dubs.human_edited=1 THEN dubs.translation ELSE excluded.translation END,draft_translation=CASE WHEN dubs.human_edited=1 THEN dubs.draft_translation ELSE excluded.draft_translation END,status=excluded.status,reasons_json=excluded.reasons_json,attempts_json=excluded.attempts_json,audio_path=excluded.audio_path,duration_s=excluded.duration_s,updated_at=CURRENT_TIMESTAMP",
        (new_id(), clip_id, lang, clip["translation"], clip["translation"], clip["status"],
         json.dumps(clip["reasons"], ensure_ascii=False), json.dumps(clip.get("translation_synthesis_attempts", [])),
         str(clip["audio_path"]), clip.get("final_audio_duration_s")),
    )


def project_row(project_id: str):
    with db.connect() as connection:
        result = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if result is None:
        raise HTTPException(404, "Project not found")
    return dict(result)


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
    """Return an H.264/AAC MP4 browser copy, preserving the uploaded original."""
    browser_copy = source.parent / "browser.mp4"
    if browser_copy.is_file() and browser_copy.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return browser_copy
    metadata = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(source)],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(metadata.stdout).get("streams", [])
    video_codec = next((item.get("codec_name") for item in streams if item.get("codec_type") == "video"), None)
    audio_codec = next((item.get("codec_name") for item in streams if item.get("codec_type") == "audio"), None)
    is_mp4 = source.suffix.lower() == ".mp4"
    if is_mp4 and video_codec == "h264" and audio_codec in {None, "aac"} and mp4_has_faststart(source):
        return source
    temporary = source.parent / "browser.part.mp4"
    try:
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                 # Bound both dimensions. A width-only cap leaves portrait phone
                 # footage (for example 1440x2560) almost full resolution and can
                 # exhaust the small memory budget available to the preview worker.
                 "-vf", "fps=24,scale=w='min(720,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                 "-threads", "1", "-threads:v", "1", "-filter_threads", "1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(temporary)],
                check=True, capture_output=True, timeout=120,
            )
            temporary.replace(browser_copy)
        except Exception:
            LOGGER.exception("Browser preview conversion failed; serving the original upload")
            return source
    finally:
        temporary.unlink(missing_ok=True)
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
    """Return a seekable H.264/AAC faststart MP4 copy when the export needs one."""
    browser_copy = source.with_name(f"{source.stem}.browser.mp4")
    if browser_copy.is_file() and browser_copy.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return browser_copy
    metadata = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(source)],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(metadata.stdout).get("streams", [])
    video_codec = next((row.get("codec_name") for row in streams if row.get("codec_type") == "video"), None)
    audio_codec = next((row.get("codec_name") for row in streams if row.get("codec_type") == "audio"), None)
    if video_codec == "h264" and audio_codec in {None, "aac"} and mp4_has_faststart(source):
        return source
    temporary = source.with_name(f"{source.stem}.browser.part.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
             "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
             "-threads", "1", "-threads:v", "1", "-filter_threads", "1",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-movflags", "+faststart", str(temporary)],
            check=True, capture_output=True, timeout=180,
        )
        temporary.replace(browser_copy)
    finally:
        temporary.unlink(missing_ok=True)
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
    db.initialize()
    with db.connect() as connection:
        connection.execute("UPDATE jobs SET state='pending', stage='resuming' WHERE state='running'")
        pending = connection.execute("SELECT id FROM jobs WHERE state='pending'").fetchall()
    for item in pending:
        asyncio.create_task(run_job(item["id"]))


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "auth_required": settings.require_access_auth}


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
        counts = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN d.status='green' THEN 1 ELSE 0 END) AS green, "
            "SUM(CASE WHEN d.status='yellow' THEN 1 ELSE 0 END) AS yellow, "
            "SUM(CASE WHEN d.status='red' THEN 1 ELSE 0 END) AS red "
            "FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id WHERE c.project_id=?",
            (project_id,),
        ).fetchone()
        jobs = connection.execute(
            "SELECT id,kind,lang,state,stage,done,total,error,created_at,updated_at "
            "FROM jobs WHERE project_id=? ORDER BY created_at DESC", (project_id,)
        ).fetchall()
    return {
        "project": {**project, "target_langs": json.loads(project["target_langs_json"])},
        "clips": {"total": int(clip_count), "dub_rows": int(counts["total"] or 0),
                  **{key: int(counts[key] or 0) for key in ("green", "yellow", "red")}},
        "jobs": [dict(row) for row in jobs],
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
        rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
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
    project_id = new_id()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO projects(id,name,source_lang,target_langs_json) VALUES(?,?,?,?)",
            (project_id, body.name, body.source_lang, json.dumps(body.target_langs)),
        )
    return {"id": project_id, "name": body.name, "status": "created"}


@app.put("/api/projects/{project_id}/video")
async def upload_video(project_id: str, video: UploadFile = File(...)) -> dict:
    project = project_row(project_id)
    suffix = Path(safe_upload_name(video.filename)).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise HTTPException(415, "Upload an MP4, MOV, or WebM video")
    folder = DATA_ROOT / "uploads" / project_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"source{suffix}"
    size = 0
    with path.open("wb") as target:
        while block := await video.read(1024 * 1024):
            size += len(block)
            if size > 500 * 1024 * 1024:
                path.unlink(missing_ok=True)
                raise HTTPException(413, "Video exceeds the 500 MB upload cap")
            target.write(block)
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
    # Preview transcoding is lazy: upload should not fail if a constrained host
    # cannot afford a second full video encode.
    with db.connect() as connection:
        connection.execute("UPDATE projects SET video_path=?,duration_s=?,status='uploaded' WHERE id=?", (str(path), duration, project_id))
    return {"filename": safe_upload_name(video.filename), "duration_s": duration, "project_id": project_id}


@app.post("/api/media/browser-preview")
async def convert_browser_preview(request: Request, video: UploadFile = File(...)):
    """Convert a pre-upload selection to a browser-playable preview, then remove it."""
    enforce_ip_limit(request, "previews")
    suffix = Path(safe_upload_name(video.filename)).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise HTTPException(415, "Upload an MP4, MOV, or WebM video")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=DATA_ROOT, prefix="browser-preview-"))
    source = folder / f"source{suffix}"
    size = 0
    try:
        with source.open("wb") as target:
            while block := await video.read(1024 * 1024):
                size += len(block)
                if size > 500 * 1024 * 1024:
                    raise HTTPException(413, "Video exceeds the 500 MB upload cap")
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
        LOGGER.exception("Browser preview conversion failed")
        raise HTTPException(422, "This video could not be converted for preview on the server. The original upload is still usable; try a smaller MP4 if preview is required.") from error


@app.get("/api/projects/{project_id}/video")
async def project_video(project_id: str, request: Request):
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(404, "Project video not found")
    try:
        playable = browser_video_path(Path(project["video_path"]))
    except Exception as error:
        LOGGER.exception("Project video preview conversion failed for project %s", project_id)
        raise HTTPException(422, "The uploaded video is saved, but this server could not prepare its preview. Try an H.264 MP4 export.") from error
    return media_response(playable, request)


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
    project_row(project_id)
    folder = DATA_ROOT / "exports" / project_id
    matches = list(folder.glob(f"*_{lang}*ai-dubbed.mp4"))
    if not matches:
        raise HTTPException(404, "Dubbed video is not ready")
    playable = browser_dubbed_video_path(matches[0])
    return media_response(playable, request)


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
        source.write_bytes(consent_bytes)
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "1", "-i", str(source), "-ac", "1", "-ar", "16000", str(consent_wav)], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            LOGGER.exception("Consent recording conversion failed")
            raise HTTPException(422, "We could not read the consent recording. Please allow microphone access and record the phrase again.") from error
        consent_bytes = consent_wav.read_bytes()
        reference_wav = Path(folder) / "reference.wav"
        if reference_audio:
            reference_source = Path(folder) / safe_upload_name(reference_audio.filename)
            reference_source.write_bytes(await reference_audio.read())
            reference_input = str(reference_source)
            audio_map = []
        else:
            reference_input = str(project["video_path"])
            audio_map = ["-map", "0:a:0", "-vn"]
        try:
            subprocess.run(
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
        reference_path.write_bytes(reference_bytes)
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
async def delete_voice(voice_id: str) -> dict:
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
async def delete_project_audio(project_id: str) -> dict:
    """Remove source and generated audio/video assets while retaining text and reports."""
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
            "SELECT c.*,d.translation,d.draft_translation,d.status,d.reasons_json,d.attempts_json,d.audio_path,d.human_edited "
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
        audio_path = item.pop("audio_path", None)
        item["audio_url"] = f"/api/clips/{item['id']}/dubs/{lang}/audio" if audio_path and Path(audio_path).is_file() else None
        item.update({
            "reasons": json.loads(row["reasons_json"] or "[]"),
            "attempts": json.loads(row["attempts_json"] or "[]"),
            "source_raw": review_by_clip.get(int(row["idx"]), {}).get("source_raw"),
            "stt_review": review_by_clip.get(int(row["idx"]), {}).get("stt_review", {}),
        })
        result.append(item)
    return result


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
    checks = run_checks(clip["src_text"], body.translation, lang, sheet, slot_seconds=clip["end_s"]-clip["start_s"])
    reasons = [f"{item['check']}: {item['detail']}" for item in checks]
    status = "red" if checks else "yellow"
    with db.connect() as connection:
        existing = connection.execute("SELECT draft_translation,translation FROM dubs WHERE clip_id=? AND lang=?", (clip_id, lang)).fetchone()
        draft_translation = (existing["draft_translation"] or existing["translation"]) if existing else body.translation
        connection.execute(
            "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,reasons_json,human_edited,audio_path,duration_s) VALUES(?,?,?,?,?,?,?,1,NULL,NULL) "
            "ON CONFLICT(clip_id,lang) DO UPDATE SET translation=excluded.translation,status=excluded.status,reasons_json=excluded.reasons_json,human_edited=1,audio_path=NULL,duration_s=NULL,updated_at=CURRENT_TIMESTAMP",
            (new_id(), clip_id, lang, body.translation, draft_translation, status, json.dumps(reasons, ensure_ascii=False)),
        )
    return {"status": status, "reasons": reasons, "human_edited": True}


async def run_job(job_id: str) -> None:
    with db.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return
        project = connection.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        voice = connection.execute("SELECT * FROM voices WHERE id=?", (job["voice_id"],)).fetchone() if job["voice_mode"] == "clone" else None
        connection.execute("UPDATE jobs SET state='running',stage='processing',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    try:
        targeted_clip_index = None
        translation_override = None
        preserve_translation = False
        if job["kind"] == "regenerate_clip":
            with db.connect() as connection:
                clip = connection.execute("SELECT * FROM clips WHERE id=? AND project_id=?", (job["clip_id"], project["id"])).fetchone()
                dub = connection.execute("SELECT translation,human_edited FROM dubs WHERE clip_id=? AND lang=?", (job["clip_id"], job["lang"])).fetchone()
            if clip is None or dub is None:
                raise ValueError("The selected clip translation is no longer available for regeneration")
            targeted_clip_index = int(clip["idx"])
            translation_override = dub["translation"]
            preserve_translation = bool(dub["human_edited"])
        targets = ([job["lang"]] if job["kind"] in {"regenerate", "regenerate_clip"} and job["lang"] else json.loads(project["target_langs_json"]))
        completed = set()
        with db.connect() as connection:
            completed = {row["lang"] for row in connection.execute("SELECT DISTINCT lang FROM dubs WHERE clip_id IN (SELECT id FROM clips WHERE project_id=?)", (project["id"],)).fetchall()}
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
            if job["kind"] in {"regenerate", "regenerate_clip"}
            or lang not in completed
            or not list(output_dir.glob(f"*_{lang}*.json"))
        ]
        if pending_targets:
            with db.connect() as connection:
                connection.execute(
                    "UPDATE jobs SET stage='transcribing and preparing clips',done=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
            # One pass shares video decode, source transcription, cleanup, and
            # segmentation across the selected languages. Previously each
            # language repeated the expensive source-audio work.
            results = await asyncio.to_thread(
                run_quality_pass,
                Path(project["video_path"]), Path(voice["reference_path"]) if voice else None, pending_targets,
                output_dir, settings, job["content_type"], job["translation_style"], embedding,
                project["source_lang"], seed_terms, job["stock_voice"] if job["voice_mode"] == "stock" else None,
                only_clip_index=targeted_clip_index, translation_override=translation_override,
                preserve_translation=preserve_translation,
            )
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
                        row = connection.execute("SELECT id FROM clips WHERE project_id=? AND idx=?", (project["id"], clip["clip"]-1)).fetchone()
                        clip_id = row["id"] if row else new_id()
                        if not row:
                            connection.execute("INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)", (clip_id, project["id"], clip["clip"]-1, clip["start_s"], clip["end_s"], clip["source_cleaned"]))
                        persist_generated_dub(connection, clip_id, lang, clip)
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='complete',stage='complete',done=total,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            connection.execute("UPDATE projects SET status='complete' WHERE id=?", (project["id"],))
    except Exception as error:
        LOGGER.exception("Dub job %s failed", job_id)
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='failed',stage='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(error)[:1000], job_id))
            connection.execute("UPDATE projects SET status='failed' WHERE id=?", (project["id"],))


@app.post("/api/projects/{project_id}/start", status_code=202)
async def start_job(request: Request, project_id: str, body: StartJob, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(409, "Upload a video first")
    if body.voice_mode not in {"clone", "stock"}:
        raise HTTPException(422, "Choose clone or stock voice mode")
    if body.voice_mode == "stock" and body.stock_voice not in STOCK_VOICES:
        raise HTTPException(422, "Choose an available Timbre stock voice")
    with db.connect() as connection:
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
        connection.execute("INSERT INTO credit_ledger(id,project_id,kind,units,est_credits) VALUES(?,?,?,?,?)", (new_id(), project_id, "dub-job-estimate", 1, credits))
    background.add_task(run_job, job_id)
    return {"job_id": job_id, "state": "pending"}


@app.get("/api/projects/{project_id}/events")
async def project_events(project_id: str):
    project_row(project_id)

    async def events():
        last = None
        while True:
            with db.connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
            if row:
                payload = {"project_id": project_id, "lang": row["lang"], "stage": row["stage"], "done": row["done"], "total": row["total"], "message": row["error"] or row["stage"]}
                serialized = json.dumps(payload)
                if serialized != last:
                    yield f"data: {serialized}\n\n"
                    last = serialized
                if row["state"] in {"complete", "failed"}:
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
    background.add_task(run_job, job_id)
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
            "SELECT c.idx,d.status,d.updated_at FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id AND d.lang=? "
            "WHERE c.project_id=? ORDER BY c.idx", (lang, project_id)
        ).fetchall()
    unresolved_red = [int(row["idx"]) + 1 for row in dubs if row["status"] == "red"]
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

    latest_edit = max((row["updated_at"] or "" for row in dubs), default="")
    stale_artifacts = []
    if latest_edit:
        try:
            from datetime import datetime, timezone
            edit_time = datetime.fromisoformat(latest_edit).replace(tzinfo=timezone.utc).timestamp()
            stale_artifacts = [name for name, path in resolved.items() if edit_time > path.stat().st_mtime + 1]
            if "audio_wav" in resolved or "audio_mp3" in resolved:
                source_mp4 = artifacts.get("mp4")
                if source_mp4 and edit_time > source_mp4.stat().st_mtime + 1 and "mp4" not in stale_artifacts:
                    stale_artifacts.append("source mp4 for audio export")
        except ValueError:
            stale_artifacts = []
    stale = bool(stale_artifacts)
    checks.append({"name": "not_stale", "passed": not stale,
                   "detail": f"Edited dub is newer than: {', '.join(stale_artifacts)}" if stale else "No newer dub edit detected"})
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
                "WHERE c.project_id=? AND d.lang=? AND d.status='red' ORDER BY c.idx",
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
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav_path)], check=True, capture_output=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_path)], check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError) as error:
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
    project_row(project_id)
    path = _language_artifacts(project_id, lang).get(download_keys[artifact])
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
    return {"language": lang, "tracks": tracks, "mix_policy": "Dubbed and quiet room tone are mixed into the MP4; source audio is available separately and is not mixed under the dub."}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    project = project_row(project_id)
    if project.get("video_path"):
        source = Path(project["video_path"]).resolve()
        upload_dir = (DATA_ROOT / "uploads" / project_id).resolve()
        if source.is_relative_to(upload_dir):
            source.unlink(missing_ok=True)
    shutil.rmtree(DATA_ROOT / "uploads" / project_id, ignore_errors=True)
    shutil.rmtree(DATA_ROOT / "exports" / project_id, ignore_errors=True)
    with db.connect() as connection:
        connection.execute("DELETE FROM dubs WHERE clip_id IN (SELECT id FROM clips WHERE project_id=?)", (project_id,))
        connection.execute("DELETE FROM clips WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM terms WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM export_events WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"deleted": True}


@app.delete("/api/data")
async def delete_all_user_data() -> dict:
    """Remove local uploads, generated exports, voice references, cached responses and database rows."""
    clear_response_cache(settings)
    for folder in ("uploads", "exports", "voices"):
        shutil.rmtree(DATA_ROOT / folder, ignore_errors=True)
    with db.connect() as connection:
        for table in ("dubs", "clips", "terms", "global_terms", "export_events", "jobs", "credit_ledger", "projects", "voices"):
            connection.execute(f"DELETE FROM {table}")
    consent_sessions.clear()
    _rate_events.clear()
    return {"deleted": True, "llm_response_cache_cleared": True}


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
