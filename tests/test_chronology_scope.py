"""Synthetic regression coverage for medical-only chronology scope."""
import io
import json
import logging

import pytest
from docx import Document

from src.chronology_agent import ChronologyAgent
from src.chronology_scope import (ScopeReviewRequired, screen_source,
                                  excluded_entry_category, parse_scoped_response)
from src.word_export import chronology_docx


def agent():
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger = logging.getLogger('scope-test')
    a.model = 'test'
    return a


MEDICAL = '06/04/2026. Clinic. Physician, MD. Office Visit. Chief Complaint: Back pain. Exam: Lumbar tenderness. Assessment: Lumbar pain. Plan: Physical therapy.'
DEPOSITION = '07/16/2026. Jamie Example, Deposition. The patient recalled back pain.'
ADMIN = [
    '07/29/2026. Clinic. Administrative Records Release Authorization. Chief Complaint: Not applicable. Plan: Release records.',
    '08/24/2026. Provider not documented. Visit Type: Life Care Planning Cost Research Report. Chief Complaint: Not documented. Plan: Projected MRI and surgery costs.',
    '08/25/2026. Clinic. Visit Type: Life Care Plan Transmission—Administrative Correspondence. History: Report sent to counsel.',
    '04/10/2026. Clinic. Administrative Life Care Planning Retainer. Plan: Pay fee.',
]


@pytest.mark.parametrize('entry', ADMIN)
def test_screenshot_entry_types_excluded(entry):
    assert excluded_entry_category(entry)


@pytest.mark.parametrize('text,category', [
    ('AUTHORIZATION TO DISCLOSE HEALTH INFORMATION\nRelease MRI records to counsel.', 'records_administration'),
    ('NOTICE OF TAKING DEPOSITION\nPlease appear on July 16, 2026.', 'legal_filing'),
    ('AMENDED COMPLAINT\nPlaintiff alleges spinal injuries and seeks damages.', 'legal_filing'),
    ('MOTION FOR SUMMARY JUDGMENT\nThe attached records show no injury.', 'legal_filing'),
    ('Subject: report\nFrom: lawyer@example.test\nAttached is the life care plan.', 'correspondence'),
    ('Life Care Planning Cost Research Report\nCPT 22551 projected surgery $100000.', 'cost_projection'),
])
def test_obvious_nonmedical_sources_are_removed_without_a_model(text, category):
    retained, log = screen_source('generic.txt', text)
    assert not retained
    assert log[0]['category'] == category


def test_medical_attachment_survives_cover_and_legal_filename():
    text = ('=== SOURCE PDF PAGE 1 ===\nCover letter\nAttached records follow.\n'
            '=== SOURCE PDF PAGE 2 ===\nClinical Evaluation\nChief Complaint: Back pain.\n'
            'Physical Examination: Lumbar tenderness.\nAssessment: Lumbar pain.\nPlan: Therapy.')
    retained, log = screen_source('legal_correspondence.txt', text)
    assert 'Cover letter' not in retained
    assert 'Lumbar tenderness' in retained
    assert len(log) == 1
    assert log[0]['extracted_text_end'] == text.index('=== SOURCE PDF PAGE 2 ===')


def test_unmarked_mixed_bundle_retained_for_semantic_screening():
    text = 'Cover letter\nAttached report.\nPhysical Examination: Normal strength.\nAssessment: Pain.'
    assert screen_source('letter.txt', text) == (text, [])


def test_substantive_physician_letter_is_not_discarded():
    text = 'Letter to counsel\nI examined the patient. MRI showed stenosis. I recommend surgery.'
    assert screen_source('correspondence.txt', text) == (text, [])


def test_medical_report_mentioning_legal_case_is_preserved():
    text = 'Independent medical evaluation\nHistory of Present Illness: Injured in collision.\nAssessment: Pain.\nPlan: Therapy. Counsel supplied a pleading.'
    assert screen_source('pleading_exhibit.txt', text) == (text, [])
    assert excluded_entry_category(MEDICAL + ' The patient discussed a motion for summary judgment.') is None
    assert excluded_entry_category(DEPOSITION) is None


def test_legal_notice_is_excluded_but_actual_deposition_survives(tmp_path):
    (tmp_path / 'notice_of_deposition.txt').write_text('NOTICE OF TAKING DEPOSITION\nAppear July 16, 2026.')
    testimony = ('IN THE CIRCUIT COURT\nDEPOSITION OF Jamie Example\nJuly 16, 2026\n'
                 + '\n'.join('Q. Pain?\nA. I recall pain.' for _ in range(8)))
    (tmp_path / 'transcript.txt').write_text(testimony)
    docs = agent()._read_extracted_files(str(tmp_path))
    assert len(docs) == 1
    assert docs[0]['document_type'] == 'deposition'
    assert docs[0]['content'] == testimony


def response(mixed=False):
    return {'sources': [{'id': 'D001', 'scope': 'mixed' if mixed else 'medical'}],
            'entries': [{'record_type': 'clinical_care', 'source_ids': ['D001'], 'text': MEDICAL}]}


def test_typed_mixed_response_omits_correspondence_and_keeps_clinical_entry():
    data = response(mixed=True)
    data['entries'].append({'record_type': 'correspondence', 'source_ids': ['D001'], 'text': ADMIN[2]})
    text, log = parse_scoped_response(json.dumps(data), [{'filename': 'bundle'}])
    assert text == MEDICAL
    assert len(log) == 1 and log[0]['category'] == 'correspondence'


def test_mislabeled_admin_entry_still_cannot_enter_clinical_chronology():
    data = response(mixed=True)
    data['entries'].append({'record_type': 'clinical_care', 'source_ids': ['D001'], 'text': ADMIN[0]})
    text, log = parse_scoped_response(json.dumps(data), [{'filename': 'bundle'}])
    assert text == MEDICAL
    assert log[0]['category'] == 'records_administration'


@pytest.mark.parametrize('mutation', ['missing_source', 'unknown_source', 'excluded_support', 'no_entry', 'unknown_type'])
def test_inconsistent_classification_stops_instead_of_silently_losing_care(mutation):
    data = response()
    if mutation == 'missing_source': data['sources'] = []
    if mutation == 'unknown_source': data['entries'][0]['source_ids'] = ['D099']
    if mutation == 'excluded_support': data['sources'][0].update(scope='excluded', category='legal_filing', reason='Pleading')
    if mutation == 'no_entry': data['entries'] = []
    if mutation == 'unknown_type': data['entries'][0]['record_type'] = 'uncertain'
    with pytest.raises(ScopeReviewRequired):
        parse_scoped_response(json.dumps(data), [{'filename': 'source'}])


def test_billing_for_actual_care_stays_and_cost_projection_does_not():
    data = response(mixed=True)
    data['entries'][0].update(record_type='medical_billing', text='06/04/2026. Clinic. Visit (billing record only). Office visit billed.')
    data['entries'].append({'record_type': 'cost_projection', 'source_ids': ['D001'], 'text': ADMIN[1]})
    text, log = parse_scoped_response(json.dumps(data), [{'filename': 'billing'}])
    assert 'Office visit billed' in text and 'Cost Research' not in text
    assert log[0]['category'] == 'cost_projection'


def test_clinical_model_prompt_and_scoped_output():
    a = agent()
    prompts = []
    a._call_api_with_retry = lambda p, **kw: prompts.append(p) or json.dumps(response())
    assert a._process_batch([{'filename': 'clinical', 'content': 'Assessment: Pain.'}], 1, 1) == MEDICAL
    assert '=== SOURCE D001: clinical ===' in prompts[0]
    assert 'HIPAA/releases' in prompts[0]
    assert 'cost-only' in prompts[0]


def test_saved_batch_gate_and_word_export(tmp_path):
    (tmp_path / 'batch_001.md').write_text('\n\n'.join([MEDICAL, DEPOSITION] + ADMIN))
    a = agent()
    result = a._combine_batches(str(tmp_path))
    assert result == MEDICAL + '\n\n' + DEPOSITION
    assert len(a._assembly_exclusions) == 4
    doc = Document(io.BytesIO(chronology_docx(result)))
    assert [p.text for p in doc.paragraphs] == [MEDICAL, DEPOSITION]


def test_all_nonmedical_sources_produce_log_and_no_fake_encounter(tmp_path):
    sources, batches, output = [tmp_path / name for name in ('sources', 'batches', 'output')]
    sources.mkdir()
    (sources / 'release.txt').write_text('HIPAA authorization\nI authorize release of my records.')
    a = agent()
    a._call_api_with_retry = lambda *args, **kw: pytest.fail('No model needed for clearly nonmedical-only input')
    assert a.generate_batches(str(sources), str(batches))['success']
    assert a.assemble_outputs(str(sources), str(batches), str(output))['success']
    result = json.loads((output / 'chronology.json').read_text())
    assert result['source_files'] == []
    assert result['all_source_files'] == ['release.txt']
    assert result['excluded_materials'][0]['category'] == 'records_administration'
    assert 'HIPAA' not in result['chronology_markdown']
    assert 'No eligible medical' in (output / 'summary.md').read_text()


def test_semantic_exclusion_empty_batch_resumes_and_is_not_used_for_header(tmp_path):
    sources, batches, output = [tmp_path / name for name in ('sources', 'batches', 'output')]
    sources.mkdir()
    (sources / 'generic.txt').write_text('Dear colleague, enclosed is the requested packet. Regards.')
    a = agent()
    a._call_api_with_retry = lambda *args, **kw: json.dumps({'sources': [
        {'id': 'D001', 'scope': 'excluded', 'category': 'correspondence', 'reason': 'Transmits packet only.'}], 'entries': []})
    assert a.generate_batches(str(sources), str(batches))['success']
    assert (batches / 'batch_001.md').read_text() == ''
    a._call_api_with_retry = lambda *args, **kw: pytest.fail('Completed exclusion should not be reprocessed')
    assert a.generate_batches(str(sources), str(batches))['batches_skipped_from_disk'] == 1
    assert a.assemble_outputs(str(sources), str(batches), str(output))['success']
    assert 'Transmits packet only' in (output / 'excluded_documents.json').read_text()


def test_old_nonmedical_chronology_is_flagged_during_verification(tmp_path):
    a = agent()
    p = tmp_path / 'draft.md'
    p.write_text(ADMIN[0])
    a._read_extracted_files = lambda _: [{'filename': 'source', 'content': '07/29/2026 authorization'}]
    a._verify_entry_batch = lambda *args: pytest.fail('Do not validate a release as medical care')
    result = a.verify_chronology(str(p), '')
    assert 'outside chronology scope' in result['verification']
    assert result['entries_unreviewed'] == 1
