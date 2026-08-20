"""Tests for consolidated-visit grouping: distinct same-date visits stay
separate, facility variants merge, facilities backfill from the source
document, and clinical records beat billing records."""

from src.assembler import (
    _backfill_facilities,
    _consolidated_key_for_fact,
    _drop_billing_when_clinical,
    _facility_root,
    _is_billing_fact,
    _merge_unknown_facility_groups,
    _sort_key,
    _visit_category,
)
from src.schemas import VerifiedFact


def _fact(**overrides) -> VerifiedFact:
    base = dict(
        source_file="doc",
        source_page=1,
        chunk_id="doc_chunk_00",
        visit_date="01/05/2024",
        facility="Sharp Rees-Stealy",
        provider_name="John Smith",
        provider_credentials="MD",
        visit_type="office visit",
        fact_category="assessment",
        finding_text="Lumbago",
        verbatim_quote="Assessment: lumbago",
        extraction_confidence="high",
        verified=True,
        verification_offset=0,
    )
    base.update(overrides)
    return VerifiedFact(**base)


class TestVisitCategory:
    def test_emergency_stays_distinct(self):
        assert _visit_category("ED visit") == "Emergency"
        assert _visit_category("Emergency Department") == "Emergency"

    def test_imaging_is_distinct(self):
        assert _visit_category("MRI lumbar spine") == "Imaging"
        assert _visit_category("X-ray right knee") == "Imaging"
        assert _visit_category("CT head") == "Imaging"

    def test_therapy_is_distinct(self):
        assert _visit_category("physical therapy") == "Therapy"
        assert _visit_category("chiropractic re-evaluation") == "Therapy"

    def test_procedure_is_distinct(self):
        assert _visit_category("operative report") == "Procedure"
        assert _visit_category("epidural steroid injection") == "Procedure"

    def test_office_visit_is_default(self):
        assert _visit_category("office visit") == "Visit"
        assert _visit_category("follow-up") == "Visit"
        # "doctor" must not trip the CT regex via substring
        assert _visit_category("doctor visit") == "Visit"


class TestFacilityRoot:
    def test_variants_share_a_root(self):
        a = _facility_root("Sharp Rees-Stealy")
        b = _facility_root("Sharp Rees-Stealy DFR")
        c = _facility_root("Sharp Rees-Stealy Medical Group")
        assert a == b == c != ""

    def test_different_facilities_differ(self):
        assert _facility_root("Sharp Rees-Stealy") != _facility_root("City Imaging")

    def test_empty_is_empty(self):
        assert _facility_root(None) == ""
        assert _facility_root("") == ""


class TestGroupingKey:
    def test_same_date_different_facilities_split(self):
        office = _fact(facility="Sharp Rees-Stealy")
        imaging_center = _fact(facility="City Imaging", visit_type="office visit")
        assert _consolidated_key_for_fact(office) != _consolidated_key_for_fact(
            imaging_center
        )

    def test_same_date_office_vs_imaging_split(self):
        office = _fact(visit_type="office visit")
        mri = _fact(visit_type="MRI lumbar spine")
        assert _consolidated_key_for_fact(office) != _consolidated_key_for_fact(mri)

    def test_facility_variants_merge(self):
        a = _fact(facility="Sharp Rees-Stealy")
        b = _fact(facility="Sharp Rees-Stealy Medical Group")
        assert _consolidated_key_for_fact(a) == _consolidated_key_for_fact(b)


class TestBackfill:
    def test_missing_facility_filled_from_same_file(self):
        named = _fact(source_file="recA", facility="Sharp Rees-Stealy")
        unnamed = _fact(source_file="recA", facility=None, visit_date="01/12/2024")
        _backfill_facilities([named, unnamed])
        assert unnamed.facility == "Sharp Rees-Stealy"

    def test_never_filled_from_other_files(self):
        named = _fact(source_file="recA", facility="Sharp Rees-Stealy")
        other = _fact(source_file="recB", facility=None)
        _backfill_facilities([named, other])
        assert other.facility is None


class TestBillingPreference:
    def test_billing_fact_detected(self):
        billing = _fact(
            fact_category="other",
            finding_text="Billing record: office visit billed",
            verbatim_quote="CPT 99213 charge amount $250",
        )
        assert _is_billing_fact(billing) is True

    def test_clinical_fact_not_billing(self):
        assert _is_billing_fact(_fact()) is False

    def test_billing_dropped_when_clinical_exists(self):
        clinical = _fact()
        billing = _fact(
            fact_category="other",
            finding_text="Billing record: office visit billed",
            verbatim_quote="CPT 99213 itemized statement",
        )
        kept, dropped = _drop_billing_when_clinical([clinical, billing])
        assert dropped == 1
        assert kept == [clinical]

    def test_billing_kept_when_only_record_for_date(self):
        billing = _fact(
            visit_date="02/02/2024",
            fact_category="other",
            finding_text="Billing record: therapy session billed",
            verbatim_quote="superbill for date of service",
        )
        kept, dropped = _drop_billing_when_clinical([billing])
        assert dropped == 0
        assert kept == [billing]


class TestUnknownFacilityMerge:
    def test_unknown_folds_into_single_named_group(self):
        named_key = ("01/05/2024", "Visit", "sharp rees")
        unknown_key = ("01/05/2024", "Visit", "")
        groups = {named_key: [_fact()], unknown_key: [_fact(facility=None)]}
        merged = _merge_unknown_facility_groups(groups)
        assert unknown_key not in merged
        assert len(merged[named_key]) == 2

    def test_unknown_stays_when_multiple_candidates(self):
        g = {
            ("01/05/2024", "Visit", "sharp rees"): [_fact()],
            ("01/05/2024", "Visit", "city imaging"): [_fact(facility="City Imaging")],
            ("01/05/2024", "Visit", ""): [_fact(facility=None)],
        }
        merged = _merge_unknown_facility_groups(g)
        assert ("01/05/2024", "Visit", "") in merged


class TestSortKey:
    def test_chronological_and_undated_last(self):
        keys = [
            ("", "Visit", ""),
            ("02/01/2024", "Visit", "b"),
            ("01/05/2024", "Visit", "a"),
            ("01/05/2024", "Imaging", "a"),
        ]
        ordered = sorted(keys, key=_sort_key)
        assert ordered[0][0] == "01/05/2024"
        assert ordered[-1][0] == ""
