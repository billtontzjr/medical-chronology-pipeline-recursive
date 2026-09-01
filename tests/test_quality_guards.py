"""Tests for the accuracy/anti-hallucination guards added in the quality
review: API stop-reason handling with refusal fallback, extraction JSON
repair + retry, clinical-priority fact capping, deterministic entry
hygiene, header-name cleanup, and the gaps.md review appendix."""

import json
from types import SimpleNamespace

import pytest

from src.anthropic_client import (
    DEFAULT_FALLBACK_MODEL,
    SECONDARY_FALLBACK_MODEL,
    AnthropicClient,
    ModelRefused,
    ResponseTruncated,
)
from src.assembler import _cap_visit_facts, _sanitize_entry
from src.extractor import _isolate_json_object, run_extraction
from src.header_extractor import _normalize_name
from src.schemas import VerifiedFact
from src.session_state import SessionStore
from src.summary_generator import _review_appendix


# --------------------------------------------------------------- helpers

def _resp(stop_reason, text="ok", with_thinking=False):
    content = []
    if with_thinking:
        content.append(SimpleNamespace(type="thinking", thinking=""))
    content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(responses):
    c = AnthropicClient(api_key="sk-test", default_model="claude-opus-5")
    c._client = SimpleNamespace(messages=_FakeMessages(responses))
    return c


def _fact(**overrides) -> VerifiedFact:
    base = dict(
        source_file="doc", source_page=1, chunk_id="doc_chunk_00",
        visit_date="09/07/2018", facility="Geisinger", provider_name="A B",
        provider_credentials="MD", visit_type="office visit",
        fact_category="assessment", finding_text="x", verbatim_quote="x",
        extraction_confidence="high", verified=True, verification_offset=0,
    )
    base.update(overrides)
    return VerifiedFact(**base)


# ------------------------------------------------------- client stop reasons

class TestClientStopReasons:
    def test_text_blocks_only_thinking_ignored(self):
        c = _client([_resp("end_turn", "hello", with_thinking=True)])
        assert c.complete("p") == "hello"

    def test_truncation_raises(self):
        c = _client([_resp("max_tokens", '{"facts": [')])
        with pytest.raises(ResponseTruncated):
            c.complete("p", max_tokens=100)

    def test_refusal_falls_back_to_opus5(self):
        c = _client([_resp("refusal", ""), _resp("end_turn", "recovered")])
        assert c.complete("p", model="claude-fable-5-1") == "recovered"
        calls = c._client.messages.calls
        assert calls[0]["model"] == "claude-fable-5-1"
        assert calls[1]["model"] == DEFAULT_FALLBACK_MODEL

    def test_refusal_on_opus5_falls_back_to_secondary(self):
        c = _client([_resp("refusal", ""), _resp("end_turn", "recovered")])
        assert c.complete("p", model="claude-opus-5") == "recovered"
        assert c._client.messages.calls[1]["model"] == SECONDARY_FALLBACK_MODEL

    def test_double_refusal_raises(self):
        c = _client([_resp("refusal", ""), _resp("refusal", "")])
        with pytest.raises(ModelRefused):
            c.complete("p", model="claude-opus-5")

    def test_no_temperature_for_new_models(self):
        c = _client([_resp("end_turn", "x"), _resp("end_turn", "y")])
        c.complete("p", model="claude-fable-5-1")
        c.complete("p", model="claude-sonnet-4-6")
        calls = c._client.messages.calls
        assert "temperature" not in calls[0]
        assert calls[1].get("temperature") == 0


# ------------------------------------------------------- extraction repair

class TestExtractionRepair:
    def test_isolate_json_object(self):
        wrapped = 'Here is the JSON:\n{"a": 1, "b": {"c": 2}}\nHope that helps.'
        assert _isolate_json_object(wrapped) == '{"a": 1, "b": {"c": 2}}'
        assert _isolate_json_object("no braces") == "no braces"

    def _valid_chunk_json(self, quote="knee pain"):
        return json.dumps({
            "source_file": "rec", "chunk_id": "rec_chunk_00",
            "chunk_start_char": 0, "chunk_end_char": 10,
            "facts": [{
                "source_file": "rec", "source_page": 1, "chunk_id": "rec_chunk_00",
                "visit_date": "01/02/2024", "facility": "Clinic",
                "provider_name": "Dr X", "provider_credentials": "MD",
                "visit_type": "office visit", "fact_category": "chief_complaint",
                "finding_text": "Knee pain", "verbatim_quote": quote,
                "extraction_confidence": "high",
            }],
            "extraction_notes": None,
        })

    def _store_with_doc(self, tmp_path):
        store = SessionStore(str(tmp_path))
        state = store.create(session_id="s1", patient_id="p", dropbox_link="/x",
                             destination_folder="/out")
        (store.extracted_dir("s1")).mkdir(parents=True, exist_ok=True)
        (store.extracted_dir("s1") / "rec.txt").write_text(
            "=== PAGE 1 ===\nPatient with knee pain seen today.", encoding="utf-8"
        )
        return store

    def test_invalid_then_valid_recovers_chunk(self, tmp_path):
        store = self._store_with_doc(tmp_path)
        client = _client([
            _resp("end_turn", "this is not json at all"),
            _resp("end_turn", "Sure!\n" + self._valid_chunk_json() + "\nDone."),
        ])
        summary = run_extraction(store, "s1", client)
        assert summary["chunks_invalid"] == 0
        assert summary["facts_emitted"] == 1

    def test_invalid_twice_is_counted(self, tmp_path):
        store = self._store_with_doc(tmp_path)
        client = _client([_resp("end_turn", "garbage"), _resp("end_turn", "garbage")])
        summary = run_extraction(store, "s1", client)
        assert summary["chunks_invalid"] == 1
        assert summary["facts_emitted"] == 0
        persisted = json.loads(
            (store.extracted_facts_dir("s1") / "rec_chunk_00.json").read_text()
        )
        assert persisted["facts"] == []
        assert persisted["extraction_notes"].startswith("validation_error")

    def test_truncation_retries_with_bigger_budget(self, tmp_path):
        store = self._store_with_doc(tmp_path)
        client = _client([
            _resp("max_tokens", '{"facts": ['),
            _resp("end_turn", self._valid_chunk_json()),
        ])
        summary = run_extraction(store, "s1", client)
        assert summary["facts_emitted"] == 1
        calls = client._client.messages.calls
        assert calls[1]["max_tokens"] == calls[0]["max_tokens"] * 2


# ------------------------------------------------------------- assembler

class TestCapPriority:
    def test_clinical_facts_survive_capping(self):
        facts = [
            _fact(fact_category="other", finding_text="admin note"),
            _fact(fact_category="patient_quote", finding_text="quote"),
            _fact(fact_category="assessment", finding_text="fracture"),
            _fact(fact_category="plan", finding_text="surgery"),
        ]
        kept, capped = _cap_visit_facts(facts, max_facts=2)
        assert capped
        assert {f.fact_category for f in kept} == {"assessment", "plan"}


class TestSanitizeEntry:
    def test_collapses_bullets_and_lines_and_markup(self):
        raw = "09/07/2018. Geisinger. A B, MD. Visit.\n- **Chief Complaint:** pain\n- Plan: rest — later"
        out = _sanitize_entry(raw, "09/07/2018")
        assert "\n" not in out and "**" not in out and "—" not in out
        assert out.startswith("09/07/2018. Geisinger. A B, MD. Visit. Chief Complaint: pain Plan: rest , later")

    def test_wrong_date_corrected(self):
        out = _sanitize_entry("9/8/2018. Clinic. Text.", "09/07/2018")
        assert out.startswith("09/07/2018. Clinic. Text.")

    def test_unpadded_same_date_normalized(self):
        out = _sanitize_entry("9/7/2018. Clinic. Text.", "09/07/2018")
        assert out.startswith("09/07/2018. Clinic.")

    def test_missing_date_prepended(self):
        out = _sanitize_entry("Clinic. Text.", "09/07/2018")
        assert out.startswith("09/07/2018. Clinic. Text.")

    def test_undated_left_alone(self):
        assert _sanitize_entry("Clinic. Text.", None) == "Clinic. Text."


# ------------------------------------------------------------ header name

class TestNormalizeName:
    def test_strips_trailing_state_code(self):
        assert _normalize_name("NANCY L SMITH TX") == "NANCY L SMITH"

    def test_strips_run_on_label(self):
        assert _normalize_name("JANE DOE DOB 01/01/1970") == "JANE DOE"

    def test_cuts_at_wide_gap(self):
        assert _normalize_name("John Smith    MRN 12345") == "John Smith"

    def test_two_token_name_kept_even_if_state_like(self):
        assert _normalize_name("Maria Ca") == "Maria Ca"

    def test_plain_name_unchanged(self):
        assert _normalize_name("John Q Public") == "John Q Public"


# --------------------------------------------------------- review appendix

class TestReviewAppendix:
    def test_lists_unparseable_segments(self, tmp_path):
        store = SessionStore(str(tmp_path))
        store.create(session_id="s1", patient_id="p", dropbox_link="/x",
                     destination_folder="/out")
        facts_dir = store.extracted_facts_dir("s1")
        facts_dir.mkdir(parents=True, exist_ok=True)
        (facts_dir / "rec_chunk_03.json").write_text(json.dumps({
            "source_file": "rec", "chunk_id": "rec_chunk_03",
            "chunk_start_char": 0, "chunk_end_char": 1, "facts": [],
            "extraction_notes": "validation_error: [...]",
        }))
        out_dir = store.output_dir("s1")
        out_dir.mkdir(parents=True, exist_ok=True)
        text = _review_appendix(store, "s1", out_dir)
        assert "not captured" in text
        assert "rec (segment rec_chunk_03)" in text

    def test_lists_cross_check_warnings(self, tmp_path):
        store = SessionStore(str(tmp_path))
        store.create(session_id="s1", patient_id="p", dropbox_link="/x",
                     destination_folder="/out")
        out_dir = store.output_dir("s1")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "chronology.md").write_text(
            "HEADER\n\n09/07/2018. Geisinger. A B, MD. Visit. "
            "Assessment: completely unsupported invented claim about zebras.\n",
            encoding="utf-8",
        )
        vfp = store.verified_facts_path("s1")
        vfp.parent.mkdir(parents=True, exist_ok=True)
        vfp.write_text(_fact(finding_text="left femur fracture",
                             verbatim_quote="femoral neck fracture").model_dump_json() + "\n")
        text = _review_appendix(store, "s1", out_dir)
        assert "flagged by cross-check" in text
        assert "zebras" in text
