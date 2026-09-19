import copy
import json

import httpx
import pytest

from src.jev_client import JevClient, JevError, DEFAULT_MODEL, questions, validate_response
from src.jev_review import audit_entries, update_issues, source_loader


def response(choice="supported", confidence=1):
    return {"model": DEFAULT_MODEL, "usage": {"input_tokens": 20}, "answers": {
        k: {"type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": {c: int(c == choice) for c in q["criteria"]}}
        for k, q in questions().items()}}


def test_http_contract_and_bounded_retry_do_not_leak_secret():
    seen, delays = [], []
    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert json.loads(request.content)["questions"] == questions()
        return httpx.Response(429, headers={"retry-after": "2"}) if len(seen) == 1 else httpx.Response(200, json=response())
    client = JevClient(api_key="synthetic-key", transport=httpx.MockTransport(handler), sleep=delays.append)
    assert client.evaluate({"entry": "fictional"}, questions())["model"] == DEFAULT_MODEL
    assert len(seen) == 2 and delays == [2]


@pytest.mark.parametrize("code", [301, 401, 422, 529])
def test_errors_redact_body_and_never_follow_redirect(code):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(code, text="synthetic-secret and confidential response", headers={"location": "https://outside.invalid"})
    client = JevClient(api_key="synthetic-secret", transport=httpx.MockTransport(handler), sleep=lambda _: None)
    with pytest.raises(JevError) as error:
        client.evaluate({}, questions())
    assert "secret" not in str(error.value) and "confidential" not in str(error.value)
    assert len(seen) == (3 if code == 529 else 1)


@pytest.mark.parametrize("mutation", [
    lambda d: d["answers"].pop("anatomy"),
    lambda d: d.update(model="jev-other"),
    lambda d: d["answers"]["anatomy"].update(confidence=float("nan")),
    lambda d: d["answers"]["anatomy"].update(choice="invented"),
    lambda d: d["answers"]["anatomy"]["probabilities"].update(supported=.1),
    lambda d: d["answers"]["anatomy"].update(choice="contradicted"),
])
def test_malformed_responses_fail_closed(mutation):
    data = response(); mutation(data)
    with pytest.raises(JevError):
        validate_response(data, questions(), DEFAULT_MODEL)


def test_large_request_never_truncates_or_calls_network():
    def forbidden(_):
        pytest.fail("Oversized evidence must not be transmitted")
    client = JevClient(api_key="fictional", transport=httpx.MockTransport(forbidden))
    with pytest.raises(JevError, match="No text was truncated"):
        client.evaluate({"source_pages": "x" * 30000}, questions())


def test_oversized_entry_does_not_block_other_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response())
    client = JevClient(api_key="fictional", transport=httpx.MockTransport(handler))
    entries = [{**ENTRIES[0], "id": "too-long", "text": "x" * 30000}, *ENTRIES]
    report = run(tmp_path, client, entries=entries)
    assert report["entries_checked"] == 1 and report["entries_unchecked"] == 1
    assert len(seen) == 1


DOCS = [{"id": "d1", "sha256": "hash", "revision": "r1"}]
ENTRIES = [{"id": "e1", "document_id": "d1", "text": "MRI was recommended.", "evidence": [
    {"document_id": "d1", "page": 1, "quote": "MRI was recommended.", "source_sha256": "hash"}]}]


class FakeClient:
    model = DEFAULT_MODEL
    def __init__(self, answer=None):
        self.calls = []; self.answer = answer or response()
    def evaluate(self, state, schema):
        self.calls.append(state)
        return copy.deepcopy(self.answer)


def run(tmp_path, client, entries=None, docs=None, text="MRI was recommended."):
    return audit_entries(entries or ENTRIES, docs or DOCS, lambda _: {1: text}, tmp_path,
                         client_factory=lambda: client)


def test_disabled_stage_makes_no_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_ENABLED", raising=False)
    client = FakeClient()
    assert run(tmp_path, client)["status"] == "disabled"
    assert not client.calls and not list(tmp_path.iterdir())


def test_complete_results_still_require_human_review_and_resume_exact_work(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    client = FakeClient()
    before = copy.deepcopy(ENTRIES)
    first = run(tmp_path, client)
    assert first["status"] == "human_review_required" and first["entries_checked"] == 1
    assert first["results"][0]["status"] == "no_flags"
    assert run(tmp_path, client) == first and len(client.calls) == 1
    assert ENTRIES == before
    run(tmp_path, client, text="MRI was recommended. Not performed.")
    assert len(client.calls) == 2
    changed = copy.deepcopy(ENTRIES); changed[0]["text"] = "MRI performed."
    run(tmp_path, client, entries=changed)
    assert len(client.calls) == 3
    run(tmp_path, client, docs=[{**DOCS[0], "revision": "r2"}])
    assert len(client.calls) == 4


@pytest.mark.parametrize("choice,confidence", [("contradicted", 1), ("insufficient_evidence", 1), ("supported", .56)])
def test_findings_and_low_confidence_remain_flags(tmp_path, monkeypatch, choice, confidence):
    monkeypatch.setenv("JEV_ENABLED", "true")
    result = run(tmp_path, FakeClient(response(choice, confidence)))
    assert result["results"][0]["status"] == "review_required"
    assert "factual_support" in result["results"][0]["flags"]


@pytest.mark.parametrize("change", [
    lambda e: e[0].update(evidence=[]),
    lambda e: e[0]["evidence"][0].update(quote="Invented source"),
    lambda e: e[0]["evidence"][0].update(source_sha256="old"),
    lambda e: e[0]["evidence"][0].update(text_revision="old"),
    lambda e: e[0]["evidence"][0].update(page=2),
    lambda e: e[0]["evidence"][0].update(evidence_origin="named_reviewer_transcription"),
])
def test_missing_stale_or_human_evidence_is_unchecked(tmp_path, monkeypatch, change):
    monkeypatch.setenv("JEV_ENABLED", "true")
    entries = copy.deepcopy(ENTRIES); change(entries)
    client = FakeClient(); report = run(tmp_path, client, entries=entries)
    assert report["status"] == "incomplete" and report["entries_unchecked"] == 1
    assert not client.calls


def test_request_limit_retains_progress_for_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setattr("src.jev_review.MAX_NEW_REQUESTS", 1)
    entries = ENTRIES + [{**ENTRIES[0], "id": "e2"}]
    client = FakeClient()
    assert run(tmp_path, client, entries=entries)["entries_unchecked"] == 1
    assert run(tmp_path, client, entries=entries)["entries_unchecked"] == 0
    assert len(client.calls) == 2


def test_missing_key_is_explicit_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    report = audit_entries(ENTRIES, DOCS, lambda _: {}, tmp_path)
    assert report["status"] == "incomplete" and "TYPESAFE_API_KEY" in report["error"]


def test_provider_failure_stops_new_calls_but_resume_can_recover(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    client = FakeClient()
    def fail(state, schema):
        client.calls.append(state)
        raise JevError("Jev unavailable.")
    client.evaluate = fail
    entries = ENTRIES + [{**ENTRIES[0], "id": "e2"}]
    assert run(tmp_path, client, entries=entries)["entries_unchecked"] == 2
    assert len(client.calls) == 1
    assert run(tmp_path, FakeClient(), entries=entries)["entries_unchecked"] == 0


def test_invalid_checkpoint_is_preserved_and_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    client = FakeClient(); run(tmp_path, client)
    path = next(tmp_path.glob("*.json")); path.write_text('{"signature":"wrong"}')
    assert run(tmp_path, client)["entries_unchecked"] == 1
    assert len(client.calls) == 1 and path.read_text() == '{"signature":"wrong"}'


def test_identity_header_page_is_included(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    entry = {**ENTRIES[0], "source_version": "hash", "identity_evidence": [{"page": 2, "quote": "Patient: Fictional"}]}
    client = FakeClient()
    report = audit_entries([entry], DOCS, lambda _: {1: "MRI was recommended.", 2: "Patient: Fictional"}, tmp_path, client_factory=lambda: client)
    assert report["entries_checked"] == 1
    assert [p["page"] for p in client.calls[0]["source_pages"]] == [1, 2]


def test_full_workspace_export_keeps_jev_flags_and_prior_versions(tmp_path, monkeypatch):
    from src.case_store import CaseStore
    from src.medical_run import MedicalRun
    from test_medical_workspace import prepared_pipeline
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUIRE_PERSISTENT_STORAGE", "false")
    monkeypatch.setenv("JEV_ENABLED", "false")
    store = CaseStore(tmp_path)
    case, pipeline, _ = prepared_pipeline(store)
    runner = MedicalRun(store, pipeline, case["id"])
    runner.run()
    old = {p: p.read_bytes() for p in (runner.path / "versions").rglob("*") if p.is_file()}
    original_entries = copy.deepcopy(store.all(case["id"], "entries"))
    fake = FakeClient(response("contradicted"))
    monkeypatch.setattr(JevClient, "__init__", lambda self: setattr(self, "model", DEFAULT_MODEL))
    monkeypatch.setattr(JevClient, "evaluate", lambda self, state, schema: fake.evaluate(state, schema))
    monkeypatch.setenv("JEV_ENABLED", "true")
    runner.run("export")
    report = json.loads((runner.path / "output" / "jev_review.json").read_text())
    assert report["entries_checked"] == 1
    assert store.overview(case["id"])["review_count"] > 0
    assert "flagged entries remain" in (runner.path / "output" / "manual_review.md").read_text()
    assert store.all(case["id"], "entries") == original_entries
    assert all(p.read_bytes() == content for p, content in old.items())
    assert "=== SOURCE PDF PAGE 1 ===" in fake.calls[0]["source_pages"][0]["text"]
    runner.run("export")
    assert len(fake.calls) == 1
    monkeypatch.setenv("JEV_ENABLED", "false")
    runner.run("export")
    assert any(i["kind"] == "jev_review" for i in store.all(case["id"], "issues"))
    docs = store.all(case["id"], "documents")
    textpath = runner.path / "extracted" / "record.txt"
    textpath.write_text(textpath.read_text() + "changed")
    with pytest.raises(JevError, match="extraction changed"):
        source_loader(store, case["id"])(docs[0])


def test_benchmark_distinguishes_labels_from_review_routing():
    from scripts.jev_benchmark import summarize
    rows = [
        {"expected": "supported", "observed": "supported", "correct": True,
         "response": {"answers": {"anatomy": {"choice": "not_applicable", "confidence": .8}}}},
        {"expected": "contradicted", "observed": "supported", "correct": False,
         "response": {"answers": {"factual_support": {"choice": "supported", "confidence": .99}}}},
        {"expected": "insufficient_evidence", "observed": "insufficient_evidence", "correct": True,
         "response": {"answers": {"factual_support": {"choice": "insufficient_evidence", "confidence": .99}}}},
    ]
    assert summarize(rows) == {"correct": 2, "total": 3, "false_accepts": 1,
        "flagged_entries": 2, "supported_entries": 1, "supported_entries_flagged": 1,
        "unsupported_entries_without_flags": 1}
