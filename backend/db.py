"""Small SQLite persistence layer for projects, assets, and resumable work."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from backend.config import get_settings

DB_PATH = Path("data/anek.db")


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
      targets_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'brain'
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
      error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
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
    """
    with connect() as db:
        db.executescript(schema)
        job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        if "content_type" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN content_type TEXT NOT NULL DEFAULT 'explainer'")
        if "translation_style" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN translation_style TEXT NOT NULL DEFAULT 'neutral'")


def json_value(value):
    return json.loads(value) if value else None
