# What belongs in the medical chronology

Include actual medical care, diagnostic testing, and substantive patient-specific
medical evaluations or opinions, including clinical IME and life-care-planning
reports. Keep the separately requested dated deposition summaries. Existing handling
of bills for actual medical services is preserved; projected future costs are not
treated as completed or ordered care.

Exclude correspondence, legal correspondence, report-transmission emails, cover
letters, pleadings, complaints, answers, motions, notices, subpoenas, discovery,
court filings/orders, retainers and fee agreements, records requests, HIPAA/release
authorizations, scheduling material, and cost-only research or projections. Medical
terminology in such material is insufficient to establish a clinical encounter.

For mixed files, retain the actual clinical report or medical attachments and omit
the administrative/legal portions. A substantive physician evaluation addressed to
counsel remains medical evidence; an attorney's allegations or paraphrase do not.
The contents of a missing attachment cannot be inferred from a transmission email.

## Enforcement and review

- The source reader removes clearly nonmedical sections conservatively. New OCR
  output retains physical PDF page markers, allowing a cover page to be omitted
  without losing the medical pages that follow. Filenames alone never exclude a
  document. Unmarked mixed files and uncertain sections continue to semantic review.
- Clinical generation must return a record type for each entry and a disposition
  for every input source. Excluded sources cannot support a medical entry. A missing
  or inconsistent disposition fails the batch rather than silently losing clinical
  material. The deposition summarizer remains a separate path.
- An additional title check rejects explicit administrative/legal/cost-only entries
  before output assembly, including those in older saved batches. Verification flags
  such entries as outside chronology scope rather than validating them as care.
- `excluded_documents.json` and the `excluded_materials` field of `chronology.json`
  record omissions with source names, categories and reasons. Source-screen offsets
  are positions in extracted text, not PDF page numbers. Exclusions are outside the
  dated chronology. `all_source_files` retains the input inventory; `source_files`
  identifies retained sources. An all-nonmedical input yields no dated encounters
  and an explicit no-eligible-entries summary plus its exclusion log.

The scope rules use the existing configured model. Semantic classification still
requires source review: tests cannot establish the model will classify every novel
or poorly scanned document correctly. Obvious source rules are intentionally
conservative to protect medical attachments. No source PDFs are edited or deleted.

Use a fresh run after deployment so all sources receive the new scope screening.
Do not resume batches made under older rules. Existing downloaded chronologies do
not update automatically. A model-backed staged case remains a release validation
step; automated tests use synthetic sources and mocked model responses.
