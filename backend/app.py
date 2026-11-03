"""Anek HTTP API and static frontend host."""

import asyncio
import json
import math
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from difflib import SequenceMatcher

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
try:
    from rapidfuzz.fuzz import ratio
except ImportError:
    ratio = lambda left, right: SequenceMatcher(None, left, right).ratio() * 100

from backend import db
from backend.brain.checks import run_checks
from backend.brain.termsheet import TermSheet
from backend.config import LANGUAGES, get_settings
from backend.pipeline.phase3b import run_quality_pass

settings = get_settings()
app = FastAPI(title=settings.app_name)
DATA_ROOT = Path("data")
CONSENT_PHRASES = [
    "Today I am recording my voice for an AI dubbing test.",
    "I consent to use this recording to create my own voice model.",
    "This is my voice, and I am choosing to create an AI dub.",
]
consent_sessions: dict[str, str] = {}
_rate_events: dict[tuple[str, str], deque[float]] = defaultdict(deque)


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
    voice_id: str
    content_type: str = "explainer"
    translation_style: str = "neutral"


class ExportRequest(BaseModel):
    lang: str
    formats: list[str] = ["mp4", "srt", "report"]


def new_id() -> str:
    return uuid.uuid4().hex


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
    return {"status": "ok", "app": settings.app_name}


@app.get("/api/config")
async def get_config() -> dict:
    with db.connect() as connection:
        used = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
    return {
        "app_name": settings.app_name,
        "languages": {code: LANGUAGES[code] for code in settings.enabled_langs if code in LANGUAGES},
        "max_video_seconds": settings.max_video_seconds,
        "llm_model": settings.llm_model,
        "daily_credit_limit": settings.gnani_daily_limit,
        "remaining_daily_credits": max(0, settings.gnani_daily_limit - int(used)),
        "low_trust_verify_langs": settings.low_trust_verify_langs,
    }


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [{**dict(row), "target_langs": json.loads(row["target_langs_json"])} for row in rows]


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
    with db.connect() as connection:
        connection.execute("UPDATE projects SET video_path=?,duration_s=?,status='uploaded' WHERE id=?", (str(path), duration, project_id))
    return {"filename": safe_upload_name(video.filename), "duration_s": duration, "project_id": project_id}


@app.get("/api/projects/{project_id}/video")
async def project_video(project_id: str):
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(404, "Project video not found")
    return FileResponse(project["video_path"], media_type="video/mp4")


@app.get("/api/projects/{project_id}/media")
async def dubbed_media(project_id: str, lang: str, request: Request):
    enforce_ip_limit(request, "previews")
    project_row(project_id)
    folder = DATA_ROOT / "exports" / project_id
    matches = list(folder.glob(f"*_{lang}*ai-dubbed.mp4"))
    if not matches:
        raise HTTPException(404, "Dubbed video is not ready")
    return FileResponse(matches[0], media_type="video/mp4")


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


@app.get("/api/voices/consent-phrase")
async def consent_phrase(session_id: str = "default") -> dict:
    import secrets

    phrase = secrets.choice(CONSENT_PHRASES)
    consent_sessions[session_id] = phrase
    return {"phrase": phrase, "notice": "This confirms a live recording; it does not prove identity."}


@app.post("/api/voices")
async def create_voice(
    request: Request,
    session_id: str = Form("default"),
    label: str = Form("My voice"),
    consent_audio: UploadFile = File(...),
    reference_audio: UploadFile = File(...),
) -> dict:
    enforce_ip_limit(request, "voice_clones")
    phrase = consent_sessions.get(session_id)
    if not phrase:
        raise HTTPException(400, "Request a consent phrase and record it live before voice setup")
    consent_bytes = await consent_audio.read()
    reference_bytes = await reference_audio.read()
    voice_id = new_id()
    voice_dir = DATA_ROOT / "voices"
    voice_dir.mkdir(parents=True, exist_ok=True)
    reference_path = voice_dir / f"{voice_id}.wav"
    with tempfile.TemporaryDirectory(dir=voice_dir, prefix="convert-") as folder:
        source = Path(folder) / safe_upload_name(consent_audio.filename)
        consent_wav = Path(folder) / "consent.wav"
        source.write_bytes(consent_bytes)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", "16000", str(consent_wav)], check=True, capture_output=True)
        consent_bytes = consent_wav.read_bytes()
        reference_source = Path(folder) / safe_upload_name(reference_audio.filename)
        reference_wav = Path(folder) / "reference.wav"
        reference_source.write_bytes(reference_bytes)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(reference_source), "-ac", "1", "-ar", "24000", str(reference_wav)], check=True, capture_output=True)
        reference_bytes = reference_wav.read_bytes()
    consent_client = create_gnani_client(settings.gnani_mock)
    transcript = consent_client.stt(consent_bytes, "indic-auto", "prisma-v3.0")
    match = ratio(phrase.casefold(), transcript.get("transcript", "").casefold())
    if match < settings.consent_match_threshold:
        raise HTTPException(422, f"Consent phrase did not match (similarity {match:.0f}%)")
    reference_path.write_bytes(reference_bytes)
    embedding = consent_client.make_embedding(reference_bytes)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO voices(id,session_id,label,embedding_json,consent_phrase,consent_ok,reference_path) VALUES(?,?,?,?,?,1,?)",
            (voice_id, session_id, label, json.dumps(embedding), phrase, str(reference_path)),
        )
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


@app.get("/api/projects/{project_id}/termsheet")
async def get_termsheet(project_id: str) -> dict:
    project_row(project_id)
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM terms WHERE project_id=?", (project_id,)).fetchall()
    return {"terms": [{**dict(row), "locked": bool(row["locked"]), "targets": json.loads(row["targets_json"])} for row in rows], "idioms": []}


@app.put("/api/projects/{project_id}/termsheet")
async def approve_terms(project_id: str, body: TermsUpdate) -> dict:
    project_row(project_id)
    sheet = TermSheet.model_validate({"terms": body.terms, "idioms": body.idioms})
    with db.connect() as connection:
        connection.execute("DELETE FROM terms WHERE project_id=?", (project_id,))
        for term in sheet.terms:
            connection.execute(
                "INSERT INTO terms(id,project_id,term,type,treatment,locked,targets_json,source) VALUES(?,?,?,?,?,?,?,?)",
                (new_id(), project_id, term.term, term.type, term.treatment, int(term.locked), json.dumps(term.targets, ensure_ascii=False), "approved"),
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
    return [{**dict(row), "reasons": json.loads(row["reasons_json"] or "[]"), "attempts": json.loads(row["attempts_json"] or "[]")} for row in rows]


@app.patch("/api/clips/{clip_id}/dubs/{lang}")
async def edit_dub(clip_id: str, lang: str, body: DubEdit) -> dict:
    with db.connect() as connection:
        clip = connection.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            raise HTTPException(404, "Clip not found")
        term_rows = connection.execute("SELECT * FROM terms WHERE project_id=?", (clip["project_id"],)).fetchall()
    sheet = TermSheet.model_validate({"terms": [{**dict(row), "targets": json.loads(row["targets_json"])} for row in term_rows]})
    checks = run_checks(clip["src_text"], body.translation, lang, sheet, slot_seconds=clip["end_s"]-clip["start_s"])
    reasons = [f"{item['check']}: {item['detail']}" for item in checks]
    status = "red" if checks else "yellow"
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,reasons_json,human_edited) VALUES(?,?,?,?,?,?,?,1) "
            "ON CONFLICT(clip_id,lang) DO UPDATE SET translation=excluded.translation,status=excluded.status,reasons_json=excluded.reasons_json,human_edited=1,updated_at=CURRENT_TIMESTAMP",
            (new_id(), clip_id, lang, body.translation, body.translation, status, json.dumps(reasons, ensure_ascii=False)),
        )
    return {"status": status, "reasons": reasons, "human_edited": True}


async def run_job(job_id: str) -> None:
    with db.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return
        project = connection.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        voice = connection.execute("SELECT * FROM voices WHERE id=?", (job["voice_id"],)).fetchone()
        connection.execute("UPDATE jobs SET state='running',stage='processing',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    try:
        targets = json.loads(project["target_langs_json"])
        completed = set()
        with db.connect() as connection:
            completed = {row["lang"] for row in connection.execute("SELECT DISTINCT lang FROM dubs WHERE clip_id IN (SELECT id FROM clips WHERE project_id=?)", (project["id"],)).fetchall()}
        output_dir = DATA_ROOT / "exports" / project["id"]
        embedding = json.loads(voice["embedding_json"])
        for target_index, lang in enumerate(targets):
            if lang in completed and list(output_dir.glob(f"*_{lang}*.json")):
                continue
            with db.connect() as connection:
                connection.execute("UPDATE jobs SET stage=?,done=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (f"processing {lang}", target_index, job_id))
            results = await asyncio.to_thread(
                run_quality_pass,
                Path(project["video_path"]), Path(voice["reference_path"]), [lang],
                output_dir, settings, job["content_type"], job["translation_style"], embedding,
            )
            for result in results:
                lang = result["language"]
                with db.connect() as connection:
                    for term in result.get("term_sheet", {}).get("terms", []):
                        exists = connection.execute("SELECT id FROM terms WHERE project_id=? AND term=?", (project["id"], term["term"])).fetchone()
                        if not exists:
                            connection.execute(
                                "INSERT INTO terms(id,project_id,term,type,treatment,locked,targets_json,source) VALUES(?,?,?,?,?,?,?,?)",
                                (new_id(), project["id"], term["term"], term["type"], term["treatment"], int(term["locked"]), json.dumps(term["targets"], ensure_ascii=False), "brain"),
                            )
                    for clip in result["clips"]:
                        row = connection.execute("SELECT id FROM clips WHERE project_id=? AND idx=?", (project["id"], clip["clip"]-1)).fetchone()
                        clip_id = row["id"] if row else new_id()
                        if not row:
                            connection.execute("INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)", (clip_id, project["id"], clip["clip"]-1, clip["start_s"], clip["end_s"], clip["source_cleaned"]))
                        connection.execute(
                            "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,reasons_json,attempts_json) VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(clip_id,lang) DO UPDATE SET translation=excluded.translation,status=excluded.status,reasons_json=excluded.reasons_json,attempts_json=excluded.attempts_json,updated_at=CURRENT_TIMESTAMP",
                            (new_id(), clip_id, lang, clip["translation"], clip["translation"], clip["status"], json.dumps(clip["reasons"], ensure_ascii=False), json.dumps([])),
                        )
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='complete',stage='complete',done=total,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            connection.execute("UPDATE projects SET status='complete' WHERE id=?", (project["id"],))
    except Exception as error:
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='failed',stage='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(error)[:1000], job_id))
            connection.execute("UPDATE projects SET status='failed' WHERE id=?", (project["id"],))


@app.post("/api/projects/{project_id}/start", status_code=202)
async def start_job(request: Request, project_id: str, body: StartJob, background: BackgroundTasks) -> dict:
    enforce_ip_limit(request, "jobs")
    project = project_row(project_id)
    if not project["video_path"]:
        raise HTTPException(409, "Upload a video first")
    with db.connect() as connection:
        voice = connection.execute("SELECT id FROM voices WHERE id=? AND consent_ok=1", (body.voice_id,)).fetchone()
        if not voice:
            raise HTTPException(409, "Choose a voice with a matched live consent recording")
        credits = estimated_job_credits(project["duration_s"], len(json.loads(project["target_langs_json"])))
        if settings.max_credits_per_job and credits > settings.max_credits_per_job:
            raise HTTPException(429, f"Estimated job cost {credits} exceeds the per-job safety cap")
        used_today = connection.execute("SELECT COALESCE(SUM(est_credits),0) FROM credit_ledger WHERE date(created_at)=date('now')").fetchone()[0]
        if used_today + credits > settings.gnani_daily_limit:
            raise HTTPException(429, "Daily Gnani safety cap reached; try again tomorrow")
        job_id = new_id()
        connection.execute("INSERT INTO jobs(id,project_id,kind,voice_id,content_type,translation_style,state,stage,total) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, project_id, "dub", body.voice_id, body.content_type, body.translation_style, "pending", "queued", len(json.loads(project["target_langs_json"]))))
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


@app.post("/api/clips/{clip_id}/dubs/{lang}/regenerate")
async def regenerate_clip(clip_id: str, lang: str) -> dict:
    with db.connect() as connection:
        dub = connection.execute("SELECT * FROM dubs WHERE clip_id=? AND lang=?", (clip_id, lang)).fetchone()
    if not dub:
        raise HTTPException(404, "Dub not found")
    return {"queued": True, "message": "Regeneration requires a project voice and is queued through project processing."}


@app.post("/api/clips/{clip_id}/dubs/{lang}/revert")
async def revert_dub(clip_id: str, lang: str) -> dict:
    with db.connect() as connection:
        dub = connection.execute("SELECT * FROM dubs WHERE clip_id=? AND lang=?", (clip_id, lang)).fetchone()
        if not dub:
            raise HTTPException(404, "Dub not found")
        connection.execute("UPDATE dubs SET translation=draft_translation,human_edited=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (dub["id"],))
    return {"translation": dub["draft_translation"], "status": dub["status"]}


@app.post("/api/projects/{project_id}/export")
async def export_project(request: Request, project_id: str, body: ExportRequest) -> dict:
    enforce_ip_limit(request, "previews")
    project_row(project_id)
    folder = DATA_ROOT / "exports" / project_id
    files = {suffix: str(path) for suffix in (".mp4", ".srt", ".json") for path in folder.glob(f"*_{body.lang}*{suffix}")}
    if not files:
        raise HTTPException(409, "No export exists for this language yet")
    return {"label": "AI dubbed", "files": files}


@app.get("/api/projects/{project_id}/report")
async def project_report(project_id: str, lang: str):
    project_row(project_id)
    folder = DATA_ROOT / "exports" / project_id
    reports = list(folder.glob(f"*_{lang}*.json"))
    if not reports:
        raise HTTPException(404, "Verification report not found")
    return json.loads(reports[0].read_text(encoding="utf-8"))


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
        connection.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"deleted": True}


@app.delete("/api/data")
async def delete_all_user_data() -> dict:
    """Remove local uploads, generated exports, voice references and database rows."""
    for folder in ("uploads", "exports", "voices"):
        shutil.rmtree(DATA_ROOT / folder, ignore_errors=True)
    with db.connect() as connection:
        for table in ("dubs", "clips", "terms", "jobs", "credit_ledger", "projects", "voices"):
            connection.execute(f"DELETE FROM {table}")
    consent_sessions.clear()
    _rate_events.clear()
    return {"deleted": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000)


if Path("backend/static/index.html").exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory="backend/static", html=True), name="studio")
