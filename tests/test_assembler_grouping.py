"""Tests for hierarchy-aware visit grouping: distinct same-date visits stay
separate (attending, consultants, imaging, care settings), facility name
variants cluster together, ancillary documentation folds into the day's
primary entry, and clinical records beat billing records."""

from src.assembler import (
    _backfill_facilities,
    _build_visit_groups,
    _cluster_facilities,
    _drop_billing_when_clinical,
    _facility_tokens,
    _imaging_modality,
    _is_ancillary_fact,
    _is_billing_fact,
    _sort_key,
    _visit_category,
)
from src.schemas import VerifiedFact


def _fact(**overrides) -> VerifiedFact:
    base = dict(
        source_file="doc",
        source_page=1,
        chunk_id="doc_chunk_00",
        visit_date="09/07/2018",
        facility="Geisinger Medical Center",
        provider_name="Aadil Maqsood",
        provider_credentials="MD",
        visit_type="Inpatient History and Physical",
        fact_category="assessment",
        finding_text="Left femur fracture",
        verbatim_quote="left femoral neck fracture",
        extraction_confidence="high",
        verified=True,
        verification_offset=0,
    )
    base.update(overrides)
    return VerifiedFact(**base)


class TestVisitCategory:
    def test_buckets(self):
        assert _visit_category("ED visit") == "Emergency"
        assert _visit_category("MRI lumbar spine") == "Imaging"
        assert _visit_category("physical therapy") == "Therapy"
        assert _visit_category("operative report") == "Procedure"
        assert _visit_category("office visit") == "Visit"
        assert _visit_category("doctor visit") == "Visit"  # no ct false-positive


class TestFacilityClustering:
    def test_variants_cluster_together(self):
        facts = [
            _fact(facility="Geisinger"),
            _fact(facility="GMC-GEISINGER MEDICAL CENTER"),
            _fact(facility="Geisinger Orthopaedics Woodbine"),
            _fact(facility="Geisinger Radiology"),
        ]
        label_of = _cluster_facilities(facts)
        assert len(set(label_of.values())) == 1

    def test_different_institutions_stay_separate(self):
        facts = [
            _fact(facility="Geisinger Medical Center"),
            _fact(facility="Mountain Laurel Healthcare and Rehabilitation Center"),
        ]
        label_of = _cluster_facilities(facts)
        assert label_of["Geisinger Medical Center"] != label_of[
            "Mountain Laurel Healthcare and Rehabilitation Center"
        ]

    def test_short_and_generic_tokens_ignored(self):
        assert "snf" not in _facility_tokens("Mt Laurel SNF")
        assert "medical" not in _facility_tokens("Geisinger Medical Center")


class TestAncillaryDetection:
    def test_nursing_and_pharmacy_are_ancillary(self):
        assert _is_ancillary_fact(
            _fact(provider_name="Tori Beveridge",
                  provider_credentials="Licensed Practical Nurse",
                  visit_type="MDS Assessment")
        )
        assert _is_ancillary_fact(
            _fact(provider_name="Sarah E Vandunk",
                  provider_credentials="PHARM Tech",
                  visit_type="Medication Reconciliation")
        )
        assert _is_ancillary_fact(
            _fact(provider_name="Lois Kephart",
                  provider_credentials="Activities Director",
                  visit_type="Activities/Recreation Therapy Note")
        )

    def test_physician_note_is_not_ancillary(self):
        assert not _is_ancillary_fact(_fact())

    def test_physician_telephone_order_is_ancillary(self):
        assert _is_ancillary_fact(
            _fact(provider_name="Dr Elkins", provider_credentials="MD",
                  visit_type="Telephone Order")
        )


class TestImagingModality:
    def test_modalities(self):
        assert _imaging_modality("X-ray Left Femur and Pelvis") == "X-ray"
        assert _imaging_modality("MRI Brain without Contrast") == "MRI"
        assert _imaging_modality("CT head") == "CT"


class TestNancySmithHierarchy:
    """Regression test modeled on the 09/07/2018 day that produced 19
    entries: hospital H&P + ortho consult + procedure + X-ray (3 variants)
    + SNF admission + a pile of ancillary notes must collapse to 5-6
    entries following the hierarchy."""

    def _day_facts(self):
        gmc = "GMC-GEISINGER MEDICAL CENTER"
        return [
            # Attending H&P (two title variants of the same document)
            _fact(visit_type="Inpatient History and Physical (H&P)"),
            _fact(facility=gmc, visit_type="Inpatient Admission History and Physical"),
            # Ortho consult
            _fact(facility="Geisinger", provider_name="Matthew Louis Chorney",
                  visit_type="Inpatient Orthopedic Consultation"),
            # Procedure note
            _fact(facility=gmc, provider_name="David Raymond Maish",
                  visit_type="Procedure Note", fact_category="procedure_performed"),
            # The same X-ray under three attributions/facility labels
            _fact(facility="Geisinger", provider_name="David Raymond Maish",
                  visit_type="X-ray Left Femur and Pelvis"),
            _fact(facility="Geisinger Radiology", provider_name="Justin Brady Bigger",
                  visit_type="X-ray Left Femur and Pelvis",
                  fact_category="imaging_finding"),
            _fact(facility=gmc, visit_type="X-ray hip"),
            # SNF admission (different institution)
            _fact(facility="Mountain Laurel SNF", provider_name="SNF Staff",
                  provider_credentials=None,
                  visit_type="Skilled Nursing Facility Admission"),
            # Ancillary noise at both settings
            _fact(facility="Mountain Laurel", provider_name="Sarah E Vandunk",
                  provider_credentials="PHARM Tech",
                  visit_type="Medication Reconciliation", fact_category="medication"),
            _fact(facility="Mountain Laurel Healthcare and Rehabilitation Center",
                  provider_name="Lois Kephart", provider_credentials="Activities Director",
                  visit_type="Activities/Recreation Therapy Note", fact_category="other"),
            _fact(facility="Mountain Laurel Healthcare and Rehabilitation Center",
                  provider_name="Tori Beveridge",
                  provider_credentials="Licensed Practical Nurse",
                  visit_type="MDS Assessment", fact_category="other"),
            _fact(facility="Geisinger Pharmacy Outpatient",
                  provider_name="Ricky Michael Rampulla Jr.",
                  provider_credentials="RPh",
                  visit_type="Outpatient Pharmacokinetic Consult",
                  fact_category="medication"),
        ]

    def test_day_collapses_to_hierarchy(self):
        groups = _build_visit_groups(self._day_facts())
        keys = sorted(groups.keys(), key=_sort_key)
        # Expected entries: Geisinger H&P (attending), ortho consult,
        # procedure, one X-ray entry, SNF admission. Ancillary all folded.
        assert len(keys) == 5, f"expected 5 entries, got {len(keys)}: {keys}"
        categories = [k[1] for k in keys]
        assert categories.count("Imaging") == 1
        assert categories.count("Procedure") == 1
        subkeys = [k[3] for k in keys]
        assert sum(1 for s in subkeys if s.startswith("consult:")) == 1
        # No standalone ancillary entries
        assert not any(s == "ancillary" for s in subkeys)

    def test_ancillary_folds_into_correct_setting(self):
        groups = _build_visit_groups(self._day_facts())
        snf_groups = {
            k: v for k, v in groups.items()
            if any("laurel" in (f.facility or "").lower() for f in v)
        }
        assert len(snf_groups) == 1
        snf_facts = list(snf_groups.values())[0]
        # MDS + activities + med rec folded into the SNF admission entry
        assert any(f.visit_type == "MDS Assessment" for f in snf_facts)
        assert any("Activities" in (f.visit_type or "") for f in snf_facts)

    def test_ancillary_only_date_gets_one_entry(self):
        facts = [
            _fact(facility="Mountain Laurel", provider_name="Tori Beveridge",
                  provider_credentials="LPN", visit_type="Nursing Note",
                  visit_date="09/10/2018"),
            _fact(facility="Mountain Laurel", provider_name="Lois Kephart",
                  provider_credentials="Activities Director",
                  visit_type="Activities Note", visit_date="09/10/2018"),
        ]
        groups = _build_visit_groups(facts)
        assert len(groups) == 1
        (key,) = groups.keys()
        assert key[3] == "ancillary"

    def test_imaging_attributed_and_separate(self):
        groups = _build_visit_groups(self._day_facts())
        imaging = [(k, v) for k, v in groups.items() if k[1] == "Imaging"]
        assert len(imaging) == 1
        _, ifacts = imaging[0]
        # All three X-ray variants merged into the one imaging entry
        assert len(ifacts) == 3
        assert any(f.fact_category == "imaging_finding" for f in ifacts)


class TestBackfill:
    def test_missing_facility_filled_from_same_file(self):
        named = _fact(source_file="recA")
        unnamed = _fact(source_file="recA", facility=None, visit_date="01/12/2024")
        _backfill_facilities([named, unnamed])
        assert unnamed.facility == "Geisinger Medical Center"

    def test_never_filled_from_other_files(self):
        named = _fact(source_file="recA")
        other = _fact(source_file="recB", facility=None)
        _backfill_facilities([named, other])
        assert other.facility is None


class TestBillingPreference:
    def test_billing_dropped_when_clinical_exists(self):
        clinical = _fact()
        billing = _fact(
            fact_category="other",
            finding_text="Billing record: office visit billed",
            verbatim_quote="CPT 99213 itemized statement",
        )
        assert _is_billing_fact(billing) and not _is_billing_fact(clinical)
        kept, dropped = _drop_billing_when_clinical([clinical, billing])
        assert dropped == 1 and kept == [clinical]

    def test_billing_kept_when_only_record_for_date(self):
        billing = _fact(
            visit_date="02/02/2024",
            fact_category="other",
            finding_text="Billing record: therapy session billed",
            verbatim_quote="superbill for date of service",
        )
        kept, dropped = _drop_billing_when_clinical([billing])
        assert dropped == 0 and kept == [billing]


class TestSortKey:
    def test_order_within_date_and_undated_last(self):
        keys = [
            ("", "Visit", "", ""),
            ("09/07/2018", "Imaging", "", "X-ray"),
            ("09/07/2018", "Visit", "geisinger", ""),
            ("09/07/2018", "Emergency", "geisinger", ""),
            ("09/07/2018", "Visit", "geisinger", "consult:chorney"),
            ("09/08/2018", "Visit", "geisinger", ""),
        ]
        ordered = sorted(keys, key=_sort_key)
        assert ordered[0][1] == "Emergency"
        assert ordered[1] == ("09/07/2018", "Visit", "geisinger", "")
        assert ordered[2][3] == "consult:chorney"
        assert ordered[3][1] == "Imaging"
        assert ordered[-1][0] == ""
