"""Explicit source-date roles and centuries, never inferred calendar pivots."""
import copy

import pytest

from src.diagnostic_dates import date_value, supports_service_date
from src.source_fidelity import render_diagnostic, DiagnosticEvidenceError


DATE = '01/12/2026'


def document(field):
    return {'content': '=== SOURCE PDF PAGE 7 ===\n' + field +
            '\nFacility: Synthetic Imaging\nProvider: Example, MD\n'
            'Study: Lumbar MRI\nImpression: No acute fracture.'}


def entry(field):
    return {'record_type': 'diagnostic_test', 'source_ids': ['D001'],
            'diagnostic_result': {'date': DATE, 'facility': 'Synthetic Imaging',
                'provider': 'Example, MD', 'study': 'Lumbar MRI',
                'evidence': [{'source_id': 'D001', 'date_quote': field,
                              'quote': 'Impression: No acute fracture.'}]}}


@pytest.mark.parametrize('field', [
    'Collected On 01/12/2026 0900', 'Collected On: 01/12/2026 09:00',
    'DATE/TIME: 1/12/2026 9:00 AM', 'Date / Time: 2026-01-12T09:00:00Z',
    'Date of examination: January 12, 2026', 'Study Date: Jan 12 2026',
    'Exam Date/Time: 01-12-2026 09:00 UTC', 'Performed On 01/12/2026',
    'Acquired: 01/12/2026', 'Collection Date: 01/12/2026',
    'Date of service:\n01/12/2026', 'Collected On\n01/12/2026 0900',
])
def test_complete_explicit_date_fields_are_source_supported(field):
    assert render_diagnostic(entry(field), {'D001': document(field)}).startswith(DATE)


@pytest.mark.parametrize('quote', [
    'Birth Date: 01/12/2026', 'DOB: 01/12/2026', 'Date of injury: 01/12/2026',
    'Report Date: 01/12/2026', 'Result Date: 01/12/2026',
    'Electronically signed on 01/12/2026', 'Date dictated: 01/12/2026',
    '01/12/2026', 'History: Exam Date: 01/12/2026',
])
def test_wrong_date_roles_and_unlabeled_or_narrative_dates_remain_blocked(quote):
    assert date_value(quote) is None
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry(quote), {'D001': document(quote)})


@pytest.mark.parametrize('field', [
    'Collected On 01/12/26 0900', 'DATE/TIME: 1/12/26 9:00 AM',
    'Exam Date: 01-12-26', 'Collected On\n01/12/26 0900',
])
def test_two_digit_year_needs_same_page_full_service_date(field):
    doc = document(field)
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry(field), {'D001': doc})
    doc['content'] = doc['content'].replace('Facility:', 'Study Date: 01/12/2026\nFacility:')
    assert render_diagnostic(entry(field), {'D001': doc}).startswith(DATE)


@pytest.mark.parametrize('corroboration', [
    'Birth Date: 01/12/2026', 'Report Date: 01/12/2026',
    'Result Date: 01/12/2026', 'History: 01/12/2026',
    'Study Date: 01/12/1926', 'Study Date: 01/13/2026',
    'Study Date: 01/12/2026\nExam Date: 01/12/1926',
])
def test_unrelated_or_conflicting_dates_cannot_supply_century(corroboration):
    quote = 'Collected On 01/12/26 0900'
    doc = document(quote + '\n' + corroboration)
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry(quote), {'D001': doc})


def test_other_page_or_carried_header_cannot_resolve_a_short_year():
    quote = 'Collected On 01/12/26 0900'
    doc = document(quote)
    doc['reference_context'] = 'Study Date: 01/12/2026'
    doc['content'] += '\n=== SOURCE PDF PAGE 8 ===\nStudy Date: 01/12/2026'
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry(quote), {'D001': doc})


@pytest.mark.parametrize('field', [
    'Date: 01/12/2026 and 01/19/2026', 'Date: 01/12/2026 Birth Date: 01/12/2026',
    'Date: 02/30/2026', 'Date: 01/12/026', 'Date: 01/12/20260',
    'Date: 01/12-2026',
])
def test_invalid_or_multiple_date_values_do_not_pass(field):
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry(field), {'D001': document(field)})


def test_full_field_cannot_hide_role_time_or_conflicting_service_date():
    quote = 'Date: 01/12/2026'
    for field in ['Birth Date: 01/12/2026', quote + ' 0900',
                  quote + '\nExam Date: 01/19/2026', quote + '\nExam Date: unreadable']:
        assert not supports_service_date(quote, DATE, field)


def test_report_date_does_not_override_a_separate_explicit_study_date():
    quote = 'Study Date: 01/12/2026'
    doc = document(quote + '\nReport Date: 01/19/2026\nDate of birth: 02/03/1980')
    assert render_diagnostic(entry(quote), {'D001': doc}).startswith(DATE)


def test_same_day_procedure_time_range_and_clinician_label_do_not_veto_exam_date():
    quote = 'DATE/TIME: 01/12/2026 6:56 PM'
    performed = 'Performed 01/12/26 1846 - 01/12/26 1856'
    doc = document(quote + '\n' + performed + '\nPerformed by: Example, MD')
    assert render_diagnostic(entry(quote), {'D001': doc}).startswith(DATE)
    assert render_diagnostic(entry(performed), {'D001': doc}).startswith(DATE)
    assert not supports_service_date('Performed by: Example, MD', DATE, doc['content'])
    assert not supports_service_date(performed, DATE, performed)


@pytest.mark.parametrize('end', ['01/13/26 1856', 'unreadable', '01/12/26 1856 and another date'])
def test_ambiguous_or_cross_day_procedure_range_still_requires_review(end):
    quote = 'DATE/TIME: 01/12/2026 6:56 PM'
    doc = document(quote + '\nPerformed 01/12/26 1846 - ' + end)
    assert not supports_service_date(quote, DATE, doc['content'])


def test_wrong_result_page_and_wrong_provider_still_fail():
    quote = 'Collected On 01/12/2026 0900'
    original = document(quote)
    for change in ('page', 'provider', 'truncated_result'):
        doc, candidate = copy.deepcopy(original), entry(quote)
        if change == 'page':
            doc['content'] = doc['content'].replace('Study:', '=== SOURCE PDF PAGE 8 ===\nStudy:')
        elif change == 'provider':
            candidate['diagnostic_result']['provider'] = 'Unrecorded, MD'
        else:
            candidate['diagnostic_result']['evidence'][0]['quote'] = 'Impression: No acute'
        with pytest.raises(DiagnosticEvidenceError):
            render_diagnostic(candidate, {'D001': doc})


@pytest.mark.parametrize('heading', ['IMPRESSION', 'Conclusion', 'Interpretation'])
def test_standalone_original_result_heading_does_not_require_invented_colon(heading):
    field = 'Study Date: 01/12/2026'
    quote = heading + '\nNo acute fracture.\nNo canal stenosis.'
    doc = document(field)
    doc['content'] = doc['content'].replace('Impression: No acute fracture.', quote)
    candidate = entry(field)
    candidate['diagnostic_result']['evidence'][0]['quote'] = quote
    assert render_diagnostic(candidate, {'D001': doc}).startswith(DATE)
    candidate['diagnostic_result']['evidence'][0]['quote'] = heading + '\nNo acute fracture.'
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(candidate, {'D001': doc})


def test_complete_impression_ends_at_the_matching_radiologist_signature():
    field = 'Study Date: 01/12/2026'
    quote = 'IMPRESSION\nNo acute fracture.\nNo canal stenosis.'
    doc = document(field)
    doc['content'] = doc['content'].replace('Provider: Example, MD\n', '')
    doc['content'] = doc['content'].replace('Impression: No acute fracture.',
        quote + '\n\nExample, M. D.\nBoard-certified radiologist\nD: 01/12/26\nT: 01/13/26')
    candidate = entry(field)
    candidate['diagnostic_result']['evidence'][0]['quote'] = quote
    assert render_diagnostic(candidate, {'D001': doc}).startswith(DATE)
    candidate['diagnostic_result']['provider'] = 'Other, MD'
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(candidate, {'D001': doc})


def test_dated_named_mri_continuation_links_to_its_own_report_header():
    field = 'DATE: 01/12/2026'
    study = 'MAGNETIC RESONANCE IMAGING OF THE LUMBAR SPINE'
    quote = 'IMPRESSION\nNo acute fracture.\nNo canal stenosis.'
    header = '=== SOURCE PDF PAGE 1 ===\nSynthetic Imaging\nPATIENT: EXAMPLE, ALEX\n' + field + '\n' + study
    continuation = '\n=== SOURCE PDF PAGE 2 ===\nPATIENT: EXAMPLE, ALEX\nEXAM: LUMBAR SPINE - MR\n' + field + '\n' + quote + '\n\nExample, M. D.'
    candidate = entry(field)
    candidate['diagnostic_result'].update(study=study)
    candidate['diagnostic_result']['evidence'][0]['quote'] = quote
    assert render_diagnostic(candidate, {'D001': {'content': header + continuation}}).startswith(DATE)
    for altered in [continuation.replace('EXAMPLE, ALEX', 'OTHER, ROBIN'),
                    continuation.replace('LUMBAR SPINE', 'CERVICAL SPINE'),
                    continuation.replace(field, 'DATE: 01/19/2026'),
                    continuation.replace(field, '')]:
        with pytest.raises(DiagnosticEvidenceError):
            render_diagnostic(candidate, {'D001': {'content': header + altered}})
