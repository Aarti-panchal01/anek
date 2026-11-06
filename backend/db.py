"""Small SQLite persistence layer for projects, assets, and resumable work."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from backend.config import get_settings

DB_PATH = Path(os.getenv("ANEK_DATA_DIR", "data")) / "anek.db"


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, source_lang TEXT NOT NULL,
      target_langs_json TEXT NOT NULL, video_path TEXT, duration_s REAL,
      status TEXT NOT NULL DEFAULT 'created', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS voices (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL, label TEXT NOT NULL,
      embedding_json TEXT NOT NULL, consent_phrase TEXT NOT NULL, consent_ok INTEGER NOT NULL,
      reference_path TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS clips (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, idx INTEGER NOT NULL,
      start_s REAL NOT NULL, end_s REAL NOT NULL, src_text TEXT NOT NULL DEFAULT '',
      src_audio_path TEXT, FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    CREATE TABLE IF NOT EXISTS terms (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, term TEXT NOT NULL,
      type TEXT NOT NULL, treatment TEXT NOT NULL, locked INTEGER NOT NULL,
      targets_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'brain',
      rule_type TEXT NOT NULL DEFAULT 'standard', heard_variants_json TEXT NOT NULL DEFAULT '[]',
      spoken_forms_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS dubs (
      id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, lang TEXT NOT NULL,
      translation TEXT NOT NULL DEFAULT '', draft_translation TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending', reasons_json TEXT NOT NULL DEFAULT '[]',
      attempts_json TEXT NOT NULL DEFAULT '[]', audio_path TEXT, audio_hash TEXT,
      duration_s REAL, human_edited INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(clip_id, lang), FOREIGN KEY(clip_id) REFERENCES clips(id)
    );
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
      lang TEXT, clip_id TEXT, voice_id TEXT, state TEXT NOT NULL DEFAULT 'pending',
      content_type TEXT NOT NULL DEFAULT 'explainer',
      translation_style TEXT NOT NULL DEFAULT 'neutral',
      voice_mode TEXT NOT NULL DEFAULT 'clone',
      stock_voice TEXT,
      error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      checkpoint_json TEXT NOT NULL DEFAULT '{}',
      stage TEXT NOT NULL DEFAULT 'queued', done INTEGER NOT NULL DEFAULT 0,
      total INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    CREATE TABLE IF NOT EXISTS credit_ledger (
      id TEXT PRIMARY KEY, project_id TEXT, kind TEXT NOT NULL, units INTEGER NOT NULL,
      est_credits REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS global_terms (
      id TEXT PRIMARY KEY, term TEXT NOT NULL, type TEXT NOT NULL,
      treatment TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0,
      targets_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
      rule_type TEXT NOT NULL DEFAULT 'standard', heard_variants_json TEXT NOT NULL DEFAULT '[]',
      spoken_forms_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(term, type)
    );
    CREATE TABLE IF NOT EXISTS export_events (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, lang TEXT NOT NULL,
      formats_json TEXT NOT NULL, unresolved_red_json TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    """
    with connect() as db:
        db.executescript(schema)
        project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
        for name, definition in (
            ("original_video_path", "TEXT"),
            ("preview_path", "TEXT"),
            ("preview_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("preview_error", "TEXT"),
        ):
            if name not in project_columns:
                db.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
        db.execute("UPDATE projects SET original_video_path=video_path WHERE original_video_path IS NULL AND video_path IS NOT NULL")
        job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        if "clip_id" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN clip_id TEXT")
        if "content_type" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN content_type TEXT NOT NULL DEFAULT 'explainer'")
        if "translation_style" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN translation_style TEXT NOT NULL DEFAULT 'neutral'")
        if "voice_mode" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN voice_mode TEXT NOT NULL DEFAULT 'clone'")
        if "stock_voice" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN stock_voice TEXT")
        if "checkpoint_json" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN checkpoint_json TEXT NOT NULL DEFAULT '{}' ")
        for table in ("terms", "global_terms"):
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in (
                ("rule_type", "TEXT NOT NULL DEFAULT 'standard'"),
                ("heard_variants_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("spoken_forms_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def json_value(value):
    return json.loads(value) if value else None
