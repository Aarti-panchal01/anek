"""Read-only project/job checkpoint diagnosis. Never prints environment values."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import db
from backend.config import LANGUAGES


def main(project_id: str) -> int:
    with db.connect() as connection:
        project = connection.execute(
            "SELECT id,name,status,target_langs_json FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not project:
            print("Project: NOT FOUND")
            return 1
        print(f"Project: {project['id']} ({project['name']})")
        print(f"Status: {project['status']}")
        targets = json.loads(project["target_langs_json"] or "[]")
        jobs = connection.execute(
            "SELECT id,kind,lang,state,stage,done,total,error,checkpoint_json FROM jobs WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        for job in jobs:
            print(f"Job: {job['id']} state={job['state']} stage={job['stage']} progress={job['done']}/{job['total']}")
            if job["error"]:
                print(f"  Last error: {str(job['error'])[:400]}")
            checkpoint = json.loads(job["checkpoint_json"] or "{}")
            print(f"  Checkpoints: {', '.join(sorted(checkpoint.keys())) or 'none'}")
        clips = connection.execute("SELECT COUNT(*) FROM clips WHERE project_id=?", (project_id,)).fetchone()[0]
        print(f"Source clips: {clips}")
        for lang in targets:
            row = connection.execute(
                "SELECT COUNT(*) AS total,SUM(CASE WHEN TRIM(d.translation)<>'' THEN 1 ELSE 0 END) AS translated "
                "FROM clips c LEFT JOIN dubs d ON d.clip_id=c.id AND d.lang=? WHERE c.project_id=?",
                (lang, project_id),
            ).fetchone()
            print(f"{LANGUAGES.get(lang, lang)}: {int(row['translated'] or 0)}/{int(row['total'] or 0)} translated clips")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/diagnose_project.py PROJECT_ID")
    raise SystemExit(main(sys.argv[1]))
