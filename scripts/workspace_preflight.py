"""Read-only, point-in-time rollout checks; never stop or resume a case."""

import argparse
import fcntl
import json
import shutil
import sqlite3
from pathlib import Path


def inspect(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Persistent data directory not found.")
    active = []
    for path in sorted((root / "locks").glob("*.lock")):
        if path.is_symlink():
            raise ValueError("Unexpected symlink in the case lock inventory.")
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active.append(path.stem)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
    database = root / "workspace.sqlite3"
    jobs = []
    version = 0
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                jobs = [
                    {"case_id": r[0], "status": r[1]}
                    for r in db.execute(
                        "SELECT case_id,status FROM jobs WHERE status IN ('queued','running')"
                    )
                ]
    roots = {
        name: (
            sorted(
                p.name
                for p in (root / name).iterdir()
                if p.is_dir() and not p.is_symlink()
            )
            if (root / name).exists()
            else []
        )
        for name in ("sessions", "medical-sessions", "archive", "medical-archive")
    }
    collisions = sorted(set(roots["sessions"]) & set(roots["medical-sessions"]))
    free = shutil.disk_usage(root).free
    return {
        "active_case_locks": active,
        "active_jobs": jobs,
        "database_schema": version,
        "case_counts": {k: len(v) for k, v in roots.items()},
        "identifier_collisions": collisions,
        "free_bytes": free,
        "ready_at_check": not (active or jobs or collisions)
        and version in (0, 1)
        and free > 300 * 1024 * 1024,
        "limitation": "Point-in-time check only. Recheck immediately before deployment; do not interrupt a worker.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir")
    args = parser.parse_args()
    result = inspect(args.data_dir)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ready_at_check"] else 2)
