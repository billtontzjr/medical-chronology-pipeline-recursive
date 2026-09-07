import io
from docx import Document
from src.word_export import chronology_docx


def test_export_preserves_entries_and_separates_only_explicit_billing():
    text = ('MEDICAL RECORDS SUMMARY\nExample\n\n'
            '03/29/2024–04/17/2024. Clinic. Therapy. C4-C5 unchanged; dose 5–10 mg.\n\n'
            '09/17/2024. Clinic. Procedure (billing record only). Billed service.\n\n'
            '09/17/2024. Clinic. Procedure note. Actual clinical findings.')
    doc = Document(io.BytesIO(chronology_docx(text, separate_billing=True)))
    result = '\n'.join(p.text for p in doc.paragraphs)
    assert '03/29/2024 to 04/17/2024' in result
    assert 'C4-C5 unchanged; dose 5–10 mg.' in result
    assert result.count('Billed service.') == 1
    assert result.index('Actual clinical findings.') < result.index('Billing-only appendix')
    assert result.index('Billed service.') > result.index('Billing-only appendix')
    original = Document(io.BytesIO(chronology_docx(text)))
    assert 'Billing-only appendix' not in '\n'.join(p.text for p in original.paragraphs)
