import pytest

from src.medical_evidence import supports_encounter_date


@pytest.mark.parametrize('quote', [
    'Date of Service: 02/05/2026 Sex: NA',
    'RE: Alex Example\nDOB: 01/03/1980\nDOE: 02/05/2026',
    'Encounter - Office Visit Date of service: 02/05/26 Patient: Alex Example DOB: 01/03/1980 PRN: TEST',
])
def test_preserved_header_quote_accepts_only_its_service_date(quote):
    page = '02/06/26, 5:15 PM\n' + quote + '\nDATE                  02/05/2026\n'
    assert supports_encounter_date(quote, '02/05/2026', page)
    assert not supports_encounter_date(quote, '02/06/2026', page)
    assert not supports_encounter_date(quote, '01/03/1980', page)
    assert not supports_encounter_date(quote.replace('02/05', '02/04'), '02/04/2026', page)


def test_generic_date_column_needs_same_page_encounter_context():
    quote = 'SEEN BY             Avery Example\n SEX           Male             DATE                02/05/2026'
    header = '02/06/26, 5:15 PM   Encounter - Office Visit Date of service: 02/05/26 Patient: Alex Example DOB: 01/03/1980\n'
    assert supports_encounter_date(quote, '02/05/2026', header + quote)
    # The older diagnostic fallback can accept DATE alone. A clinical recovery
    # quote spanning other header columns must not borrow context elsewhere.
    assert not supports_encounter_date(quote, '02/05/2026', quote)
    assert not supports_encounter_date(quote, '02/05/2026', header.replace('02/05/26 ', '02/04/26 ') + quote)


@pytest.mark.parametrize('quote', [
    'DOB: 02/05/2026',
    'Date of Injury: 02/05/2026',
    'Printed Date: 02/05/2026',
    'History: Date of Service: 02/05/2026',
    'Date of Service: 02/05/2026 through 02/08/2026',
    'Date of Service: 02/05/2026 or 02/08/2026',
    'Date of Service: 02/05/20260 Sex: NA',
    'Date of Service: 02/30/2026 Sex: NA',
])
def test_header_recovery_does_not_invent_dates(quote):
    assert not supports_encounter_date(quote, '02/05/2026', quote)


def test_short_header_year_needs_reliable_century():
    quote = 'Encounter - Office Visit Date of service: 02/05/26 Patient: Alex Example DOB: 01/03/1980'
    assert not supports_encounter_date(quote, '02/05/2026', quote)
    assert supports_encounter_date(quote, '02/05/2026', quote, ['04/22/2025'])
    assert not supports_encounter_date(quote, '02/05/2026', quote, ['04/22/1925', '04/22/2025'])
