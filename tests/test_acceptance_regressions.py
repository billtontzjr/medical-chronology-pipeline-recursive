"""Regressions derived from fictional live acceptance records, never patient data."""
import asyncio
import copy
import json
import logging
from types import SimpleNamespace

import pytest

from src.chronology_scope import ScopeReviewRequired, parse_scoped_response
from src.pipeline import MedicalChronologyPipeline
from src.scope_recovery import screen_batch
from src.session_model import batch_signature, saved_model
from src.session_state import SessionStore

SOURCE = """=== SOURCE PDF PAGE 1 ===
Date of service: 01/12/2026
Facility: Synthetic Beta Imaging
Provider: Bert Example, MD
Record type: Lumbar radiographs
Impression: Preserved vertebral body heights. No acute fracture.
=== SOURCE PDF PAGE 2 ===
Date of service: 01/19/2026
Facility: Synthetic Beta Imaging
Provider: Bert Example, MD
Record type: Lumbar MRI
Impression: Small L4-5 disc bulge without spinal canal stenosis.
"""
DOCS = [{"filename": "Nested/Imaging/visit.txt", "content": SOURCE}]
EXPECTED = ("01/19/2026. Synthetic Beta Imaging. Bert Example, MD. Lumbar MRI. "
            "Impression: Small L4-5 disc bulge without spinal canal stenosis.")


def response():
    return {
        "sources": [{"id": "D001", "scope": "medical"}],
        "entries": [{
            "record_type": "diagnostic_test",
            "source_ids": ["D001"],
            "diagnostic_result": {
                "date": "01/19/2026",
                "facility": "Synthetic Beta Imaging",
                "provider": "Bert Example, MD",
                "study": "Lumbar MRI",
                "evidence": [{
                    "source_id": "D001",
                    "date_quote": "Date of service: 01/19/2026",
                    "quote": "Impression: Small L4-5 disc bulge without spinal canal stenosis.",
                }],
            },
        }],
    }


def test_diagnostic_entry_is_rendered_from_source_quotes():
    text, excluded = parse_scoped_response(json.dumps(response()), DOCS)
    assert text == EXPECTED
    assert excluded == []
    assert "Plan:" not in text and "ongoing low back pain" not in text


@pytest.mark.parametrize("field,value", [
    ("provider", "Unrecorded Example, MD"),
    ("facility", "Unrecorded Imaging Center"),
    ("study", "Lumbar MRI performed for ongoing low back pain"),
    ("date", "01/12/2026"),
])
def test_unanchored_diagnostic_header_is_rejected(field, value):
    data = response()
    data["entries"][0]["diagnostic_result"][field] = value
    with pytest.raises(ScopeReviewRequired, match="Diagnostic"):
        parse_scoped_response(json.dumps(data), DOCS)


@pytest.mark.parametrize("change", ["invented_plan", "wrong_page", "missing_evidence",
                                  "unknown_source", "free_narrative", "extra_field"])
def test_unsupported_diagnostic_output_is_not_published(change):
    data = response()
    entry = data["entries"][0]
    result = entry["diagnostic_result"]
    if change == "invented_plan":
        result["evidence"][0]["quote"] += " Results forwarded to the referring provider."
    elif change == "wrong_page":
        result["evidence"][0]["date_quote"] = "Date of service: 01/12/2026"
    elif change == "missing_evidence":
        result["evidence"] = []
    elif change == "unknown_source":
        result["evidence"][0]["source_id"] = "D099"
    elif change == "free_narrative":
        entry["text"] = EXPECTED + " Plan: Results forwarded to the referring provider."
    else:
        result["plan"] = "Follow up with referring provider."
    with pytest.raises(ScopeReviewRequired, match="Diagnostic"):
        parse_scoped_response(json.dumps(data), DOCS)


def test_previously_generated_imaging_fabrications_fail_closed_without_evidence(tmp_path):
    data = response()
    entry = data["entries"][0]
    del entry["diagnostic_result"]
    entry["text"] = ("01/19/2026. Synthetic Beta Imaging. Bert Example, MD. MRI of the Lumbar Spine. "
                     "Lumbar MRI performed for ongoing low back pain following the 01/09/2026 injury. "
                     "Impression: Small L4-5 disc bulge without spinal canal stenosis. "
                     "Plan: Results forwarded to the referring provider for correlation with clinical findings.")
    calls = []
    checkpoint = tmp_path / "screen.json"

    def call(*args, **kwargs):
        calls.append(1)
        return json.dumps(data)

    for _ in range(2):
        with pytest.raises(ScopeReviewRequired):
            screen_batch("prompt", DOCS, call, checkpoint=checkpoint, allow_manual_review=True)
    assert calls == [1]
    work = json.loads(checkpoint.read_text())
    assert work["status"] == "blocked"
    assert work["attempts"][0]["code"] == "diagnostic_source_review"


def test_diagnostic_quote_whitespace_is_not_new_information():
    docs = copy.deepcopy(DOCS)
    docs[0]["content"] = SOURCE.replace("without spinal canal", "without\nspinal   canal")
    assert parse_scoped_response(json.dumps(response()), docs)[0] == EXPECTED


def test_same_impression_on_another_page_cannot_supply_wrong_study():
    data = response()
    data["entries"][0]["diagnostic_result"]["study"] = "Lumbar radiographs"
    with pytest.raises(ScopeReviewRequired, match="Diagnostic"):
        parse_scoped_response(json.dumps(data), DOCS)


def test_injury_date_is_not_a_diagnostic_service_date():
    data = response()
    result = data["entries"][0]["diagnostic_result"]
    result["date"] = "01/09/2026"
    result["evidence"][0]["date_quote"] = "Date of injury: 01/09/2026"
    docs = [{"filename": DOCS[0]["filename"],
             "content": SOURCE.replace("Date of service: 01/19/2026",
                                       "Date of injury: 01/09/2026\nDate of service: 01/19/2026")}]
    with pytest.raises(ScopeReviewRequired, match="Diagnostic"):
        parse_scoped_response(json.dumps(data), docs)


@pytest.mark.parametrize("change", ["date_substring", "provider_other_page", "truncated_result"])
def test_quotes_cannot_hide_a_date_role_or_move_or_truncate_results(change):
    data, docs = response(), copy.deepcopy(DOCS)
    result = data["entries"][0]["diagnostic_result"]
    if change == "date_substring":
        docs[0]["content"] = SOURCE.replace("Date of service: 01/19/2026", "Birth Date: 01/19/2026")
        result["evidence"][0]["date_quote"] = "Date: 01/19/2026"
    elif change == "provider_other_page":
        docs[0]["content"] = SOURCE.replace("Bert Example, MD", "Another Example, MD", 1)
        result["provider"] = "Another Example, MD"
    else:
        result["evidence"][0]["quote"] = "Impression: Small L4-5 disc bulge"
    with pytest.raises(ScopeReviewRequired, match="Diagnostic"):
        parse_scoped_response(json.dumps(data), docs)


def test_same_day_consolidation_cannot_rewrite_diagnostic_quotes(tmp_path):
    from src.encounters import consolidate
    clinical = ("01/19/2026. Synthetic Beta Imaging. Bert Example, MD. Office visit. "
                "Assessment: Mechanical low back pain.")
    entries, merges = consolidate([clinical, EXPECTED],
                                 lambda *a, **k: pytest.fail("No diagnostic rewrite"),
                                 tmp_path / "merge.json")
    assert entries == [clinical, EXPECTED]
    assert merges == []


def test_legacy_model_is_recovered_without_reusing_old_rule_batches(tmp_path):
    legacy_signature = batch_signature([DOCS], [], "gpt-6-astra", version="same-day-care-v3")
    assert legacy_signature != batch_signature([DOCS], [], "gpt-6-astra")
    manifest = {"signature": legacy_signature, "model": "gpt-6-astra"}
    (tmp_path / "batch_manifest.json").write_text(json.dumps(manifest))
    agent = SimpleNamespace(model="claude-opus-5", _read_extracted_files=lambda _: DOCS,
                            _plan_batches=lambda _: [DOCS])
    assert saved_model(agent, tmp_path, tmp_path, ["gpt-6-astra"]) == "gpt-6-astra"
    assert json.loads((tmp_path / "batch_manifest.json").read_text()) == manifest


def test_old_generation_batches_are_preserved_and_cannot_be_reused(tmp_path):
    from src.chronology_agent import ChronologyAgent
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.model = "gpt-6-astra"
    a._read_extracted_files = lambda _: DOCS
    a._call_api_with_retry = lambda *a, **k: pytest.fail("Never generate over old results")
    (tmp_path / "batch_manifest.json").write_text(json.dumps({
        "signature": batch_signature([DOCS], [], a.model, version="same-day-care-v3")}))
    old = tmp_path / "batch_001.md"
    old.write_text("Preserved original draft")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="summary rules"):
        a.generate_batches(str(tmp_path), str(tmp_path))
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_refresh_creates_new_pending_session_and_preserves_old_files(tmp_path):
    p = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    p.store = SessionStore(str(tmp_path))
    p.chronology_agent = SimpleNamespace(model="gpt-6-astra")
    p.logger = logging.getLogger("synthetic-refresh")
    original = p.create_session("/SYNTHETIC-only", "synthetic")
    original_output = p.store.output_dir(original.session_id) / "chronology.md"
    original_output.write_text("Preserved synthetic original.")
    from src.session_model import save_model_config
    save_model_config(p.store.batches_dir(original.session_id), "gpt-6-astra")
    before = {path.relative_to(p.store.session_dir(original.session_id)): path.read_bytes()
              for path in p.store.session_dir(original.session_id).rglob("*") if path.is_file()}
    refreshed = p.refresh_session(original.session_id)
    assert refreshed.session_id != original.session_id
    assert refreshed.dropbox_link == original.dropbox_link
    assert refreshed.destination_folder != original.destination_folder
    assert refreshed.status == "pending"
    assert all(phase.status == "pending" for phase in refreshed.phases.values())
    assert json.loads((p.store.batches_dir(refreshed.session_id) / "run_model.json").read_text())["model"] == "gpt-6-astra"
    assert not list(p.store.input_dir(refreshed.session_id).iterdir())
    assert not list(p.store.extracted_dir(refreshed.session_id).iterdir())
    assert not list(p.store.output_dir(refreshed.session_id).iterdir())
    assert {path.relative_to(p.store.session_dir(original.session_id)): path.read_bytes()
            for path in p.store.session_dir(original.session_id).rglob("*") if path.is_file()} == before


def test_model_pin_does_not_replace_a_different_saved_model(tmp_path):
    from src.session_model import save_model_config
    save_model_config(tmp_path, "gpt-6-astra")
    before = (tmp_path / "run_model.json").read_bytes()
    with pytest.raises(ValueError):
        save_model_config(tmp_path, "claude-opus-5")
    assert (tmp_path / "run_model.json").read_bytes() == before


def test_refresh_recovers_legacy_model_without_mutating_old_checkpoint(tmp_path):
    p = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    p.store = SessionStore(str(tmp_path))
    p.chronology_agent = SimpleNamespace(model="claude-opus-5",
                                        _read_extracted_files=lambda _: DOCS,
                                        _plan_batches=lambda _: [DOCS])
    original = p.create_session("/SYNTHETIC-only", "synthetic")
    batches = p.store.batches_dir(original.session_id)
    (batches / "batch_manifest.json").write_text(json.dumps({
        "signature": batch_signature([DOCS], [], "gpt-6-astra", version="same-day-care-v3")}))
    (batches / "batch_001.md").write_text("Preserved legacy draft")
    before = {path.name: path.read_bytes() for path in batches.iterdir()}
    refreshed = p.refresh_session(original.session_id, model_candidates=["gpt-6-astra"])
    assert {path.name: path.read_bytes() for path in batches.iterdir()} == before
    assert json.loads((p.store.batches_dir(refreshed.session_id) / "run_model.json").read_text())["model"] == "gpt-6-astra"
    # A programmatic caller also cannot accidentally run with the sidebar model.
    state_before = p.store.state_path(refreshed.session_id).read_bytes()
    assert "differs from the saved run model" in asyncio.run(p.run(refreshed.session_id))["error"]
    assert "differs from the saved run model" in p.verify_session(refreshed.session_id)["error"]
    assert p.store.state_path(refreshed.session_id).read_bytes() == state_before


def test_generation_prompt_does_not_require_invented_fields(monkeypatch):
    from src.chronology_agent import ChronologyAgent
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.model = "synthetic"
    a.logger = logging.getLogger("synthetic-prompt")
    captured = []

    def screen(prompt, *args, **kwargs):
        captured.append(prompt)
        return "", []

    monkeypatch.setattr("src.chronology_agent.screen_batch", screen)
    a._call_api_with_retry = lambda *a, **k: pytest.fail("No live calls")
    a._process_scoped_batch(DOCS, 1, 1)
    prompt = captured[0]
    assert "ALWAYS include the Assessment and Plan in every entry" not in prompt
    assert "diagnostic_result" in prompt and "date_quote" in prompt
    assert "only when documented" in prompt
