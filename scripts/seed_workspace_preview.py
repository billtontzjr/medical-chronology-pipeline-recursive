"""Create only fictional local preview fixtures with real, inspectable PDF pages."""

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reportlab.pdfgen import canvas
from reportlab.lib.utils import simpleSplit
from src.case_store import CaseStore, MEDICAL_POLICY, digest
from src.deposition_evidence import atomic_json
from src.word_export import chronology_docx


def seed(root):
    os.environ["SESSION_DATA_DIR"] = str(Path(root).resolve())
    os.environ["REQUIRE_PERSISTENT_STORAGE"] = "false"
    store = CaseStore(Path(__file__).resolve().parents[1])
    if store.list_cases():
        raise ValueError(
            "Use an empty preview data directory; existing cases are preserved."
        )
    names = [
        ("preview-morgan", "Alex Morgan", "complete", 3),
        ("preview-rivera", "Jordan Rivera", "in_progress", 1),
        ("preview-taylor", "Casey Taylor", "complete", 0),
        ("preview-chen", "Sam Chen", "paused", 2),
    ]
    for sid, name, status, count in names:
        state = store.sessions.create(
            sid,
            name,
            "https://www.dropbox.com/fictional-preview",
            "/Medical chronology pipeline outputs/" + sid,
        )
        state.status = status
        for key, p in state.phases.items():
            p.status = (
                "complete"
                if status == "complete" or key in ("download", "ocr")
                else "pending"
            )
        store.sessions.save(state)
        store.sessions.input_dir(sid)
        store.sessions.extracted_dir(sid)
        atomic_json(store.directory(sid) / "policy.json", MEDICAL_POLICY)
        store.put(
            sid,
            "case",
            "metadata",
            {
                "name": name,
                "dob": "04/14/1980",
                "doi": "02/03/2026",
                "model": "claude-opus-5",
                "created_policy": MEDICAL_POLICY,
            },
        )
        documents = [
            (
                "Office evaluation.pdf",
                "included",
                "Clinical evaluation",
                "02/05/2026",
                "Patient: "
                + name
                + "\nDate of birth: 04/14/1980\nDate of service: 02/05/2026\nProvider: Avery Example, MD\nFacility: Sample Orthopedic Clinic\nHistory: Neck pain following the reported collision. No arm weakness reported.\nExamination: Cervical tenderness; upper extremity strength 5/5.\nImpression: Cervical strain.\nPlan: Physical therapy and follow-up in four weeks.",
            ),
            (
                "Cervical MRI.pdf",
                "included",
                "Diagnostic study",
                "02/18/2026",
                "Patient: "
                + name
                + "\nDate of birth: 04/14/1980\nStudy date: 02/18/2026\nCervical MRI\nFacility: Example Imaging\nProvider: Cameron Example, MD\nImpression: Small C5-6 disc protrusion without spinal canal stenosis. No cord signal abnormality.",
            ),
            (
                "Therapy progress.pdf",
                "included",
                "Therapy records",
                "03/04/2026",
                "Patient: "
                + name
                + "\nDate of birth: 04/14/1980\nDate of service: 03/04/2026\nProvider: Riley Example, DPT\nHistory: Neck pain improved to 3/10.\nTreatment: Cervical mobility and home exercise program reviewed.\nPlan: Continue therapy twice weekly for two weeks.",
            ),
            (
                "Scanned follow-up.pdf",
                "needs_review",
                "Page 2 needs a clearer extraction",
                "03/11/2026",
                "Patient: "
                + name
                + "\nDate of service: 03/11/2026\nFollow-up examination.\nThe second page is intentionally unreadable in this fictional preview.",
            ),
            (
                "Referral identity.pdf",
                "needs_review",
                "Patient details need confirmation",
                "03/15/2026",
                "Patient: Different Example\nDate of service: 03/15/2026\nReferral for orthopedic evaluation.",
            ),
            (
                "Attorney cover letter.pdf",
                "excluded",
                "Legal correspondence; no clinical report in this file",
                "",
                "Dear counsel:\nEnclosed please find the requested materials.\nAdministrative cover letter only.",
            ),
            (
                "Deposition transcript.pdf",
                "excluded",
                "Deposition testimony is outside medical-only scope",
                "",
                "DEPOSITION TRANSCRIPT\nQ. Please state your name.\nA. Fictional Witness.\nThis is fictional preview material.",
            ),
            (
                "Duplicate MRI.pdf",
                "duplicate",
                "Identical copy retained in the source inventory",
                "02/18/2026",
                "",
            ),
        ]
        doc_ids = []
        for n, (filename, classification, reason, date, text) in enumerate(documents):
            docid = digest([sid, filename])[:24]
            doc_ids.append(docid)
            pdf = store.directory(sid) / "input" / filename
            if classification == "duplicate":
                pdf.write_bytes((pdf.parent / "Cervical MRI.pdf").read_bytes())
                text = documents[1][4]
            else:
                c = canvas.Canvas(str(pdf), pagesize=(612, 792))
                for page in range(1, 3 if n == 3 else 2):
                    c.setFillColorRGB(0.08, 0.19, 0.25)
                    c.setFont("Helvetica-Bold", 16)
                    c.drawString(54, 738, "FICTIONAL SOURCE RECORD")
                    c.setFont("Helvetica", 10)
                    c.setFillColorRGB(0.35, 0.4, 0.43)
                    c.drawString(
                        54, 716, "Interface preview only - no real patient data"
                    )
                    y = 674
                    c.setFillColorRGB(0.1, 0.1, 0.1)
                    c.setFont("Times-Roman", 12)
                    for line in text.splitlines():
                        for wrapped in simpleSplit(line, "Times-Roman", 12, 490):
                            c.drawString(54, y, wrapped)
                            y -= 20
                    c.setFont("Helvetica", 9)
                    c.drawCentredString(306, 35, str(page))
                    c.showPage()
                c.save()
            page_count = 2 if n == 3 else 1
            txt = "=== SOURCE PDF PAGE 1 ===\n" + text
            (
                store.directory(sid) / "extracted" / Path(filename).with_suffix(".txt")
            ).write_text(txt)
            doc = {
                "id": docid,
                "path": filename,
                "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "status": classification,
                "reason": reason,
                "page_count": page_count,
                "pages": [
                    {"page": p, "status": "error" if n == 3 and p == 2 else "text"}
                    for p in range(1, page_count + 1)
                ],
            }
            if classification == "duplicate":
                doc.update(duplicate_of=doc_ids[1], duplicate_kind="exact")
            store.put(sid, "documents", docid, doc)
            if n < 3:
                provider = [
                    "Avery Example, MD",
                    "Cameron Example, MD",
                    "Riley Example, DPT",
                ][n]
                clinical = [
                    "History: Neck pain following the reported collision without arm weakness. Examination: Cervical tenderness with upper extremity strength 5/5. Impression: Cervical strain. Plan: Physical therapy and follow-up in four weeks.",
                    "Impression: Small C5-6 disc protrusion without spinal canal stenosis. No cord signal abnormality.",
                    "History: Neck pain improved to 3/10. Treatment: Cervical mobility exercises and the home exercise program were reviewed. Plan: Continue therapy twice weekly for two weeks.",
                ][n]
                entry = {
                    "id": f"{docid}-entry",
                    "document_id": docid,
                    "date": date,
                    "sort_date": date[-4:] + date[:2] + date[3:5],
                    "record_type": [
                        "clinical_care",
                        "diagnostic_test",
                        "clinical_care",
                    ][n],
                    "text": f"{date}. "
                    + [
                        "Sample Orthopedic Clinic",
                        "Example Imaging",
                        "Sample Rehabilitation",
                    ][n]
                    + f". {provider}. "
                    + ["Office evaluation. ", "Cervical MRI. ", "Physical therapy. "][n]
                    + clinical,
                    "evidence": [
                        {
                            "page": 1,
                            "quote": text,
                            "document_id": docid,
                            "source_sha256": doc["sha256"],
                        }
                    ],
                }
                store.put(sid, "entries", entry["id"], entry)
        for n, (kind, title, reason, docn) in enumerate(
            [
                (
                    "ocr",
                    "Check the unreadable follow-up page",
                    "Page 2 did not produce readable text. Retry its extraction or compare the original before deferring.",
                    3,
                ),
                (
                    "patient_identity",
                    "Confirm the patient on this referral",
                    "The name differs from this case. This document is withheld until a reviewer confirms its association.",
                    4,
                ),
                (
                    "duplicate",
                    "Compare the duplicate imaging report",
                    "An identical MRI copy was found. Both originals are retained; the chronology uses one encounter.",
                    7,
                ),
            ][:count]
        ):
            issue = {
                "id": f"issue-{n}",
                "document_id": doc_ids[docn],
                "kind": kind,
                "title": title,
                "reason": reason,
                "status": "open",
                "page": 2 if n == 0 else 1,
            }
            issue["fingerprint"] = digest(issue)
            store.put(sid, "issues", issue["id"], issue)
        entries = store.all(sid, "entries")
        header = (
            "MEDICAL RECORDS SUMMARY\n"
            + name.upper()
            + "\nDate of Birth: 04/14/1980\nDate of Injury: 02/03/2026\n\n"
        )
        text = (
            header
            + "Fictional preview only. Draft—human source review required.\n\n"
            + "\n\n".join(
                e["text"] for e in sorted(entries, key=lambda e: e["sort_date"])
            )
        )
        output = store.directory(sid) / "versions" / "fictional-preview"
        output.mkdir(parents=True)
        (output / "chronology.docx").write_bytes(
            chronology_docx(text, template="underlined")
        )
        (output / "chronology.md").write_text(text)
        atomic_json(output / "chronology.json", {"records": entries, "preview": True})
        for file in output.iterdir():
            store.put(
                sid,
                "artifacts",
                "preview-" + file.name,
                {
                    "id": "preview-" + file.name,
                    "name": file.name,
                    "version": "fictional-preview",
                    "section": "versions",
                    "path": "fictional-preview/" + file.name,
                    "bytes": file.stat().st_size,
                },
            )
        store.put(sid, "case", "export", {"version": "fictional-preview"})
    print("Fictional preview seeded at", root)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root")
    seed(p.parse_args().root)
