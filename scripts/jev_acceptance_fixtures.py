"""Fictional holdout, authored before live evaluation; never tune against its results.

Engineering gate: no unsupported entry without a review flag and at most one
of six supported entries flagged. Label accuracy and per-dimension errors are
reported separately. This small gate is not clinical validation.
"""
# Labels are authored from the text, not model responses. Scope order:
# factual_support, date_role, attribution, negation, anatomy, procedure_status.
S = 'supported'
C = 'contradicted'
I = 'insufficient_evidence'
N = 'not_applicable'
CASES = [
    ('dates', '''SYNTHETIC RECORD. Patient: Morgan Fiction. Harbor Example Clinic.
Date of service: 06/11/2026. Note signed: 06/14/2026.
The patient attended the office evaluation on June 11; June 14 is only the signature date.
They reported right wrist soreness after gardening. No numbness was reported.
The clinician recommended a removable wrist brace. Follow-up was offered if symptoms persisted.''',
     ('The office evaluation occurred on 06/11/2026.', [S,S,N,N,N,N]),
     ('The office evaluation occurred on 06/14/2026.', [C,C,N,N,N,N])),
    ('amendment', '''SYNTHETIC RECORD. Patient: Casey Fiction. Office examination.
Initial dictation described left ankle swelling. An addendum corrects that dictation:
Swelling was confined to the RIGHT ankle. The left ankle had no swelling.
Both feet were warm. The patient walked independently and described no knee symptoms.
The addendum supersedes only the side of the swelling; the remaining examination is unchanged.''',
     ('The amended examination records right ankle swelling.', [S,N,N,S,S,N]),
     ('The amended examination records left ankle swelling.', [C,N,N,C,C,N])),
    ('procedure', '''SYNTHETIC RECORD. Patient: Robin Fiction. Treatment planning note.
An ultrasound-guided injection of the left shoulder was recommended after discussion.
Authorization was requested. The procedure has not been performed and no medication was injected.
The patient wished to consider the recommendation. A home exercise handout was supplied.
The separate authorization form is administrative and is not a procedure report.''',
     ('A left shoulder injection was recommended.', [S,N,N,N,S,S]),
     ('A left shoulder injection was performed.', [C,N,N,N,S,C])),
    ('attribution', '''SYNTHETIC RECORD. Patient: Jamie Fiction. History interview.
The patient reported that their mother had undergone lumbar fusion.
The patient specifically denied any personal history of spine surgery.
No operative records for the mother were supplied. The clinician recorded this as family history.
The current visit addressed intermittent neck discomfort; no surgery was planned.''',
     ('The patient reported a family history of lumbar fusion in their mother.', [S,N,S,N,S,S]),
     ('The patient underwent lumbar fusion.', [C,N,C,N,S,C])),
    ('negation', '''SYNTHETIC RECORD. Patient: Taylor Fiction. Follow-up visit.
The patient denied tingling in either hand. They reported occasional stiffness after typing.
Examination documented symmetric grip and normal light-touch sensation.
The clinician discussed changing workstation height and taking regular breaks.
No new test results were reviewed at this visit.''',
     ('The patient denied tingling in either hand.', [S,N,S,S,S,N]),
     ('The patient reported tingling in both hands.', [C,N,S,C,S,N])),
    ('missing_findings', '''SYNTHETIC RECORD. Patient: Avery Fiction. Imaging scheduling record.
A cervical MRI was scheduled for 07/22/2026. This record was created before that date.
The record contains scheduling details only: arrival time, parking and instructions to remove jewelry.
It contains no imaging report, interpretation or description of a disc herniation.
Completion of the scan is not documented in the supplied material.''',
     ('A cervical MRI was scheduled for 07/22/2026.', [S,S,N,N,S,S]),
     ('The cervical MRI demonstrated a C5-C6 disc herniation.', [I,N,N,I,I,I])),
]
KEYS = ('factual_support','date_role','attribution','negation','anatomy','procedure_status')
FIXTURES = [(name + '_' + kind, source, claim, labels[0])
            for name, source, good, bad in CASES
            for kind, (claim, labels) in [('supported', good), ('unsupported', bad)]]
EXPECTED_DIMENSIONS = {name + '_' + kind: dict(zip(KEYS, labels))
                       for name, source, good, bad in CASES
                       for kind, (claim, labels) in [('supported', good), ('unsupported', bad)]}
