"""Supervised, single-host durable worker. Browser lifetime never owns a job."""

import os
import signal
import threading
import time
import uuid
from pathlib import Path

from .case_store import CaseStore
from .medical_run import MedicalRun
from .session_state import PauseRequested
from .workspace_api import WorkspaceAPI


def execute_job(store, job, pipeline_factory, stopping=lambda: False):
    finished = threading.Event()

    def heartbeat():
        while not finished.wait(15):
            with store.connect() as db:
                db.execute(
                    "UPDATE jobs SET heartbeat=? WHERE id=? AND owner=? AND status=?",
                    (time.time(), job["id"], job["owner"], "running"),
                )

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with store.lock(job["case_id"]):
            case = store.overview(job["case_id"])
            pipeline = pipeline_factory(case["model"])

            def progress(message):
                store.update_job(job, message=message)

            result = MedicalRun(
                store, pipeline, job["case_id"], progress, stopping
            ).run(job["action"])
            store.update_job(
                job, "complete", **{k: v for k, v in result.items() if k != "status"}
            )
    except PauseRequested as exc:
        state = store.sessions.load(job["case_id"])
        state.status = "paused"
        store.sessions.save(state)
        store.update_job(job, "paused", message=str(exc))
    except Exception as exc:
        # A lock conflict must never change an active worker's state file.
        if "already being processed" in str(exc):
            store.update_job(
                job,
                "paused",
                message="This case already has an active worker. Its saved work is unchanged.",
            )
        else:
            try:
                store.directory(job["case_id"])
                state = store.sessions.load(job["case_id"])
                state.status = "failed"
                state.last_error = str(exc)
                for phase in state.phases.values():
                    if phase.status == "in_progress":
                        phase.status = "failed"
                        phase.error = str(exc)
                store.sessions.save(state)
            except (OSError, ValueError):
                pass  # Corrupt/missing cases remain available for recovery; no phantom case is created.
            store.update_job(job, "failed", message=str(exc))
    finally:
        finished.set()
        thread.join(timeout=2)


def main():
    base = Path(__file__).resolve().parents[1]
    api = WorkspaceAPI(base)
    store = api.store
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    owner = str(os.getpid()) + ":" + uuid.uuid4().hex
    while not stopped.is_set():
        job = store.claim(owner)
        if job:
            execute_job(store, job, api.pipeline, stopped.is_set)
        else:
            stopped.wait(2)


if __name__ == "__main__":
    main()
