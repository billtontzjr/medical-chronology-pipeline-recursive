import pytest
from src.medical_evidence import injury_date_from_source, injury_date_evidence


@pytest.mark.parametrize('field', [
    'Auto Accident - Date: Apr 22, 2025; Place: California; Qualifier: Initial Treatment',
    'Date of Injury: 04/22/2025',
    'DOI: 04/22/25',
])
def test_injury_field_preserves_date_and_source_quote(field):
    refs = injury_date_evidence(field, ['05/06/2025'])
    assert [x['date'] for x in refs] == ['04/22/2025']
    assert refs[0]['quote'] in field
    assert injury_date_from_source(field, ['05/06/2025']) == '04/22/2025'


@pytest.mark.parametrize('field', [
    'DOB: 04/22/2025', 'Date of Visit: 04/22/2025',
    'Previous injury date: 04/22/2025',
    'Date of Injury: 04/22/2025 or 04/25/2025',
    'Date of Injury: 04/22/20250', 'DOI: 04/31/2025',
])
def test_unrelated_ambiguous_or_invalid_injury_date_is_not_promoted(field):
    assert injury_date_from_source(field) == ''


def test_conflicting_injury_fields_are_retained_without_selecting_one():
    text = 'DOI: 04/22/2025\nDate of Injury: 04/25/2025'
    assert injury_date_from_source(text) == ''
    assert {r['date'] for r in injury_date_evidence(text)} == {'04/22/2025', '04/25/2025'}
    assert injury_date_from_source('DOI: 04/22/25') == ''


def test_injury_heading_does_not_call_missing_metadata_absent_or_hide_conflicts():
    from src.medical_run import injury_header
    assert injury_header([{}]) == 'Not established from verified source fields'
    assert injury_header([{'doi': '04/22/2025'}]) == 'April 22, 2025'
    conflict = injury_header([{'doi': '', 'doi_evidence': [
        {'date': '04/22/2025', 'quote': 'DOI: 04/22/2025'},
        {'date': '04/25/2025', 'quote': 'DOI: 04/25/2025'},
    ]}])
    assert 'Conflicting source dates' in conflict
    assert '04/22/2025' in conflict and '04/25/2025' in conflict
