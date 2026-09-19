"""Additional review of final entries against full, version-checked cited pages.

This stage never rewrites entries or replaces source coverage and human review.
"""

import hashlib
import json
from pathlib import Path

from .case_store import digest
from .deposition_evidence import atomic_json
from .jev_client import JevClient, JevError, PROTOCOL, enabled, questions, validate_response
from .ocr_coverage import coverage_path
from .source_pages import page_units

REVIEW_THRESHOLD = .90  # Review-routing heuristic, not a measured medical accuracy rate.
MAX_NEW_REQUESTS = 500
CHECK_LABELS = {"factual_support": "factual wording", "date_role": "encounter dates",
                "attribution": "patient and provider attribution", "negation": "symptom presence or absence",
                "anatomy": "body side and location", "procedure_status": "planned versus completed care"}
NOTICE = (
    "Jev is an additional automated review of final entries against cited source pages. "
    "It does not certify medical accuracy, correct OCR, check every uncited page, establish "
    "missing-encounter coverage, or replace human review. Flagged entries remain in the draft. "
    "Confidence is a model statistic, not a guarantee of correctness."
)


def source_loader(store, case_id):
    """Only use this case's recorded originals and exact extraction revisions."""
    loaded = {}

    def load(document):
        identifier = document["id"]
        if identifier in loaded:
            return loaded[identifier]
        original = store.file(case_id, "input", document["path"])
        if hashlib.sha256(original.read_bytes()).hexdigest() != document["sha256"]:
            raise JevError("Original source version changed; reconcile the source before Jev review.")
        textfile = store.file(case_id, "extracted", str(Path(document["path"]).with_suffix(".txt")))
        raw = textfile.read_text()
        text_hash = hashlib.sha256(raw.encode()).hexdigest()
        reportfile = coverage_path(original, store.directory(case_id) / "input", store.directory(case_id) / "extracted")
        report = json.loads(reportfile.read_text())
        if (text_hash != document.get("text_sha256") or text_hash != report.get("text_sha256")
                or report.get("source_sha256") != document["sha256"]):
            raise JevError("Source extraction changed; rerun source processing before Jev review.")
        pages = {}
        for number, text in page_units(raw):
            if number is not None:
                if number in pages:
                    raise JevError("Source page numbering is ambiguous.")
                pages[number] = text
        override = store.get(case_id, "overrides", identifier) or {}
        if override and override.get("source_sha256") != document["sha256"]:
            raise JevError("A source correction belongs to an older original.")
        for number, text in override.get("corrections", {}).items():
            pages[int(number)] = f"=== SOURCE PDF PAGE {number} ===\n" + text
        loaded[identifier] = pages
        return pages
    return load


def evidence_state(entry, documents, load):
    refs = list(entry.get("evidence", []))
    if not refs or not entry.get("text", "").strip():
        raise JevError("The entry has no usable source references or text.")
    # Identity can be cited on a separate header page. These references are stored
    # relative to the entry's original document by the existing evidence validator.
    refs.extend({**ref, "document_id": entry.get("document_id"),
                 "source_sha256": entry.get("source_version")}
                for ref in entry.get("identity_evidence", []))
    pages, links = {}, []
    for ref in refs:
        doc = documents.get(ref.get("document_id"))
        page = ref.get("page")
        if not doc or ref.get("source_sha256") != doc["sha256"] or type(page) is not int or page < 1:
            raise JevError("A source reference is missing or belongs to an older original.")
        if ref.get("text_revision") and ref["text_revision"] != doc.get("revision"):
            raise JevError("An entry cites an older extraction revision; rerun source processing before Jev review.")
        if ref.get("evidence_origin") == "named_reviewer_transcription":
            raise JevError("This human transcription requires original-page review; Jev cannot validate it from OCR alone.")
        source = load(doc).get(page)
        if not source or not ref.get("quote", "").strip():
            raise JevError("A cited source page or quotation is unavailable.")
        if " ".join(ref["quote"].split()) not in " ".join(source.split()):
            raise JevError("A stored quotation no longer matches its source page.")
        key = (doc["id"], page)
        pages[key] = {"document_id": doc["id"], "page": page, "text": source}
        links.append({"document_id": doc["id"], "page": page, "source_sha256": doc["sha256"],
                      "revision": doc.get("revision"), "page_sha256": hashlib.sha256(source.encode()).hexdigest()})
    return {"entry": entry["text"], "source_pages": [pages[k] for k in sorted(pages)]}, links


def audit_entries(entries, documents, load, checkpoint_dir, progress=lambda _: None, client_factory=JevClient):
    base = {"protocol": PROTOCOL, "notice": NOTICE, "threshold": REVIEW_THRESHOLD,
            "entries_total": len(entries), "entries_checked": 0, "entries_unchecked": len(entries), "results": []}
    if not enabled():
        return {**base, "status": "disabled"}
    try:
        client = client_factory()
    except JevError as exc:
        return {**base, "status": "incomplete", "error": str(exc)}
    base["model"] = client.model
    schema = questions()
    docs = {doc["id"]: doc for doc in documents}
    sent = 0
    api_failure = None
    for index, entry in enumerate(entries, 1):
        progress(f"Jev source review: entry {index} of {len(entries)}")
        item = {"entry_id": entry["id"], "document_id": entry.get("document_id"),
                "text": entry.get("text", ""), "status": "unchecked", "source_refs": []}
        try:
            state, links = evidence_state(entry, docs, load)
            item["source_refs"] = links
            signature = digest([PROTOCOL, client.model, schema, REVIEW_THRESHOLD, entry, state, links])
            item["signature"] = signature
            checkpoint = Path(checkpoint_dir) / (signature + ".json")
            response = None
            if checkpoint.exists():
                try:
                    saved = json.loads(checkpoint.read_text())
                    if not isinstance(saved, dict) or saved.get("signature") != signature:
                        raise JevError("Checkpoint signature mismatch.")
                    response = validate_response(saved["response"], schema, client.model)
                except (ValueError, KeyError, JevError):
                    raise JevError("A saved Jev checkpoint is invalid; preserve it for investigation before retrying.") from None
            if response is None:
                if api_failure:
                    raise JevError(api_failure)
                if sent >= MAX_NEW_REQUESTS:
                    raise JevError("This run reached its Jev request limit. Resume to check the remaining entries.")
                sent += 1
                try:
                    response = validate_response(client.evaluate(state, schema), schema, client.model)
                except JevError as exc:
                    # A single provider failure must not trigger hundreds of doomed calls.
                    api_failure = str(exc)
                    raise
                atomic_json(checkpoint, {"signature": signature, "response": response})
            item["answers"] = response["answers"]
            item["usage"] = response["usage"]
            item["flags"] = [key for key, answer in response["answers"].items()
                             if answer["choice"] not in ("supported", "not_applicable")
                             or answer["confidence"] < REVIEW_THRESHOLD]
            item["status"] = "review_required" if item["flags"] else "no_flags"
            base["entries_checked"] += 1
        except JevError as exc:
            item["reason"] = str(exc)
        except (OSError, ValueError, KeyError, TypeError):
            # Files may contain PHI. Never place raw parser/file exceptions in reports.
            item["reason"] = "Source evidence could not be loaded safely; this entry was not checked by Jev."
        base["results"].append(item)
    base["entries_unchecked"] = len(entries) - base["entries_checked"]
    base["status"] = "incomplete" if base["entries_unchecked"] or not entries else "human_review_required"
    return base


def report_markdown(report):
    lines = ["# Jev source review", "", report["notice"], "", "Status: " + report["status"],
             f"Entries checked: {report['entries_checked']} of {report['entries_total']}. "
             f"Entries not checked: {report['entries_unchecked']}."]
    if report.get("model"):
        lines.append("Model: " + report["model"])
    if report.get("error"):
        lines.extend(["", report["error"]])
    if report["status"] == "disabled":
        lines.extend(["", "Jev was disabled for this export. No records were sent to TypeSafe by this stage."])
    for item in report["results"]:
        lines.extend(["", "## Entry " + item["entry_id"], "", item["text"], "", "Result: " + item["status"]])
        if item.get("reason"):
            lines.append(item["reason"])
        for key, answer in item.get("answers", {}).items():
            lines.append(f"- {CHECK_LABELS[key]}: {answer['choice'].replace('_', ' ')} (confidence {answer['confidence']:.2f})")
        for ref in item["source_refs"]:
            lines.append(f"- Source {ref['document_id']}, physical PDF page {ref['page']}; page SHA-256 {ref['page_sha256']}.")
    return "\n".join(lines) + "\n"


def update_issues(store, case_id, report):
    """Rebuild derived Jev flags, retaining a deferral for the identical finding."""
    if report["status"] == "disabled":
        return  # Disabling the provider cannot resolve its previous findings.
    prior = {i["id"]: i for i in store.all(case_id, "issues") if i.get("kind") == "jev_review"}
    items = []
    for result in report["results"]:
        if result["status"] == "no_flags":
            continue
        docs = sorted({r["document_id"] for r in result["source_refs"]} or
                      ({result["document_id"]} if result.get("document_id") else set()))
        for doc in docs:
            items.append({"id": "jev-" + digest([result["entry_id"], doc])[:24],
                          "document_id": doc, "pages": sorted({r["page"] for r in result["source_refs"] if r["document_id"] == doc}),
                          "proposed_entry": {"text": result["text"]}, "entry_id": result["entry_id"],
                          "reason": result.get("reason") or "Review Jev flags: " + ", ".join(CHECK_LABELS[k] for k in result["flags"]) + ". The entry remains in the draft.",
                          "fingerprint": digest(result)})
    if report.get("error") or (report["status"] == "incomplete" and not report["results"]):
        items.append({"id": "jev-unavailable", "reason": report.get("error", "No entries are available for Jev review."),
                      "fingerprint": digest(report), "action": "export"})
    with store.connect() as db:
        for identifier in prior:
            if not report.get("error"):
                db.execute("DELETE FROM records WHERE case_id=? AND kind='issues' AND id=?", (case_id, identifier))
        for item in items:
            item.update(kind="jev_review", title="Jev source check needs review", status="open")
            if item.get("pages"):
                item["page"] = item["pages"][0]
            if prior.get(item["id"], {}).get("fingerprint") == item["fingerprint"]:
                item["status"] = prior[item["id"]].get("status", "open")
            store.put(case_id, "issues", item["id"], item, db)
