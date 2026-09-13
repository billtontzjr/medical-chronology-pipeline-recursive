"""Transactional workspace metadata beside, never instead of, original session files."""

import contextlib
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from .deposition_evidence import atomic_json
from .session_state import SessionStore, validate_session_id
from .session_lock import session_lock

SCHEMA = 1
MEDICAL_POLICY = {
    "version": "medical-only-v1",
    "depositions": False,
    "billing_only": False,
    "medical_expert_reports": True,
    "template": "classic",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CaseStore:
    def __init__(self, base_dir):
        self.sessions = SessionStore(str(base_dir))
        self.root = self.sessions.sessions_root.parent
        self.sessions.sessions_root = self.root / "medical-sessions"
        self.sessions.sessions_root.mkdir(exist_ok=True)
        self.sessions._sessions_root_resolved = self.sessions.sessions_root.resolve()
        self.path = self.root / "workspace.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA:
                raise RuntimeError(
                    "Workspace data uses a newer version. Restore the matching application release."
                )
            db.executescript("""
              CREATE TABLE IF NOT EXISTS records (
                case_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                data TEXT NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(case_id, kind, id));
              CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, target TEXT NOT NULL,
                data TEXT NOT NULL, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, action TEXT NOT NULL,
                status TEXT NOT NULL, owner TEXT, heartbeat REAL, created REAL NOT NULL,
                data TEXT NOT NULL);
              CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(case_id)
                WHERE status IN ('queued','running');
              PRAGMA user_version=1;
            """)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def directory(self, case_id, *, archived=False):
        case_id = validate_session_id(case_id)
        roots = [
            self.root / ("medical-archive" if archived else "medical-sessions"),
            self.root / ("archive" if archived else "sessions"),
        ]
        matches = [root for root in roots if (root / case_id).exists()]
        if len(matches) > 1:
            raise ValueError(
                "A case identifier conflicts across storage versions. Both copies are preserved for recovery."
            )
        root = matches[0] if matches else roots[0]
        path = root / case_id
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Invalid case location.")
        if not path.is_dir():
            raise FileNotFoundError("Case not found.")
        return path

    def file(self, case_id, section, relative):
        if section not in ("input", "extracted", "output", "versions"):
            raise ValueError("Invalid case resource.")
        root = self.directory(case_id) / section
        if not root.resolve().is_relative_to(self.directory(case_id).resolve()):
            raise FileNotFoundError("Case resource points outside this case.")
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise FileNotFoundError("Source not found in this case.")
        return path

    def put(self, case_id, kind, identifier, data, db=None):
        if db is None:
            with self.connect() as db:
                self.put(case_id, kind, identifier, data, db)
            return
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(case_id,kind,id) "
            "DO UPDATE SET data=excluded.data,updated=excluded.updated",
            (case_id, kind, identifier, json.dumps(data), time.time()),
        )

    def get(self, case_id, kind, identifier):
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM records WHERE case_id=? AND kind=? AND id=?",
                (case_id, kind, identifier),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def all(self, case_id, kind):
        with self.connect() as db:
            rows = db.execute(
                "SELECT data FROM records WHERE case_id=? AND kind=? ORDER BY id",
                (case_id, kind),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def overview(self, case_id, *, archived=False):
        path = self.directory(case_id, archived=archived)
        try:
            state = json.loads((path / "state.json").read_text())
        except (OSError, ValueError):
            return {
                "id": case_id,
                "name": case_id,
                "status": "recovery_required",
                "archived": archived,
                "legacy": True,
                "review_count": 1,
                "error": "Saved case state cannot be read. Its original files are preserved.",
            }
        policy = (
            json.loads((path / "policy.json").read_text())
            if (path / "policy.json").exists()
            else None
        )
        metadata = self.get(case_id, "case", "metadata") or {}
        issues = self.all(case_id, "issues")
        job = self.job(case_id)
        return {
            **state,
            **metadata,
            "id": case_id,
            "name": metadata.get("name") or state.get("patient_id") or case_id,
            "policy": policy,
            "legacy": policy is None,
            "archived": archived,
            "review_count": sum(i["status"] in ("open", "deferred") for i in issues),
            "review_status": (
                "needs_review"
                if any(i["status"] in ("open", "deferred") for i in issues)
                else "not_signed_off"
            ),
            "job": job,
            "documents_count": len(self.all(case_id, "documents")),
            "entries_count": len(self.all(case_id, "entries")),
        }

    def list_cases(self, archived=False):
        roots = [
            self.root / ("medical-archive" if archived else "medical-sessions"),
            self.root / ("archive" if archived else "sessions"),
        ]
        cases = []
        paths = [p for root in roots if root.exists() for p in root.iterdir()]
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                try:
                    cases.append(self.overview(path.name, archived=archived))
                except (ValueError, OSError) as exc:
                    cases.append(
                        {
                            "id": path.name,
                            "name": path.name,
                            "status": "recovery_required",
                            "archived": archived,
                            "error": str(exc),
                            "review_count": 1,
                            "legacy": True,
                        }
                    )
        return sorted(cases, key=lambda c: c.get("updated_at", ""), reverse=True)

    def create(self, pipeline, *, name, dob="", doi="", source, model, policy=None):
        if not name.strip():
            raise ValueError("Enter the patient name.")
        for label, value in (("Date of birth", dob), ("Date of injury", doi)):
            if value.strip():
                try:
                    datetime.strptime(value.strip(), "%m/%d/%Y")
                except ValueError:
                    raise ValueError(label + " must use MM/DD/YYYY.")
        selected = {**MEDICAL_POLICY, **(policy or {})}
        if (
            selected["version"] != MEDICAL_POLICY["version"]
            or selected["depositions"] is not False
        ):
            raise ValueError("New cases use the medical-records-only policy.")
        if (
            type(selected["billing_only"]) is not bool
            or type(selected["medical_expert_reports"]) is not bool
        ):
            raise ValueError("Invalid scope options.")
        if selected["template"] not in ("classic", "underlined", "plain"):
            raise ValueError("Unknown Word template.")
        pipeline.store = self.sessions
        state = pipeline.create_session(source, name)
        path = self.directory(state.session_id)
        atomic_json(path / "policy.json", selected)
        self.put(
            state.session_id,
            "case",
            "metadata",
            {
                "name": name.strip(),
                "dob": dob.strip(),
                "doi": doi.strip(),
                "model": model,
                "created_policy": selected,
                "schema": SCHEMA,
            },
        )
        return self.overview(state.session_id)

    def lock(self, case_id):
        locks = self.root / "locks"
        locks.mkdir(exist_ok=True)
        return session_lock(locks / (validate_session_id(case_id) + ".lock"))

    def archive(self, case_id, restore=False):
        with self.lock(case_id):
            job = self.job(case_id)
            if job and job["status"] in ("queued", "running"):
                raise ValueError("Pause the active job before archiving this case.")
            source = self.directory(case_id, archived=restore)
            medical = source.parent.name.startswith("medical-")
            target = (
                self.root
                / (
                    ("medical-" if medical else "")
                    + ("sessions" if restore else "archive")
                )
                / case_id
            )
            target.parent.mkdir(exist_ok=True)
            if target.exists():
                raise ValueError(
                    "A case with this identifier already exists in the destination."
                )
            source.rename(target)
        return self.overview(case_id, archived=not restore)

    def enqueue(self, case_id, action="run"):
        self.directory(case_id)
        if self.overview(case_id).get("legacy"):
            raise ValueError("Historical runs remain in the legacy workspace.")
        if action not in ("run", "export"):
            raise ValueError("Unknown job action.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM jobs WHERE case_id=? AND status IN ('queued','running')",
                (case_id,),
            ).fetchone()
            if not active:
                self.sessions.clear_pause(case_id)
            try:
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        case_id,
                        action,
                        "queued",
                        None,
                        None,
                        time.time(),
                        "{}",
                    ),
                )
            except sqlite3.IntegrityError:
                pass  # Repeated Resume returns the same active job.
        return self.job(case_id)

    def job(self, case_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE case_id=? ORDER BY created DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def claim(self, owner, stale_after=120):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Case file locks remain the final authority, including legacy workers.
            db.execute(
                "UPDATE jobs SET status='queued',owner=NULL WHERE status='running' AND heartbeat<?",
                (time.time() - stale_after,),
            )
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running',owner=?,heartbeat=? WHERE id=?",
                (owner, time.time(), row["id"]),
            )
            return {**dict(row), "owner": owner, "status": "running"}

    def update_job(self, job, status="running", **data):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,heartbeat=?,data=? WHERE id=? AND owner=?",
                (status, time.time(), json.dumps(data), job["id"], job["owner"]),
            )

    def decision(
        self, case_id, target, action, reviewer, reason, fingerprint, payload=None
    ):
        if not reviewer.strip() or not reason.strip():
            raise ValueError("Enter your name and the reason for this decision.")
        event = {
            "id": uuid.uuid4().hex,
            "target": target,
            "action": action,
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "fingerprint": fingerprint,
            "payload": payload or {},
            "created": time.time(),
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?)",
                (event["id"], case_id, target, json.dumps(event), event["created"]),
            )
        return event

    def history(self, case_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT data FROM decisions WHERE case_id=? ORDER BY created DESC",
                (case_id,),
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def replace_document_result(self, case_id, document, entries, issues):
        """One document revision cannot erase unrelated encounters or review work."""
        with self.connect() as db:
            for kind in ("entries", "issues"):
                rows = db.execute(
                    "SELECT id,data FROM records WHERE case_id=? AND kind=?",
                    (case_id, kind),
                ).fetchall()
                for row in rows:
                    if json.loads(row["data"]).get("document_id") == document["id"]:
                        db.execute(
                            "DELETE FROM records WHERE case_id=? AND kind=? AND id=?",
                            (case_id, kind, row["id"]),
                        )
            self.put(case_id, "documents", document["id"], document, db)
            for kind, values in (("entries", entries), ("issues", issues)):
                for item in values:
                    self.put(case_id, kind, item["id"], item, db)
