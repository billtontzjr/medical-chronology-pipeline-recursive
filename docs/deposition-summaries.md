# Deposition summaries in the medical chronology

New runs summarize each readable deposition as one paragraph in chronological
order using its session date:

`MM/DD/YYYY. Witness Name, credentials if documented, Deposition. Summary...`

The paragraph identifies the witness and summarizes relevant testimony, admissions,
history, treatment response, symptoms, function and future-care discussions. It
retains the witness's uncertainty and attribution. Historical events recalled in
testimony stay within the deposition paragraph. Clinical records remain separate
dated encounters. No examination or treatment-plan labels are added to testimony.

A combined file may have an AI-generated summary before its transcript. The reader
isolates the transcript at a supported heading/cover boundary before any chunking.
A table-of-contents mention is insufficient. A wrapper disclaimer does not exclude
the actual testimony. Summary-only files, unclear boundaries, unsupported dates or
names, and missing evidence stop generation with a source-review message.

The model cites stable source-line ranges instead of retyping long OCR quotations.
The app retrieves the exact text and rejects missing, out-of-range, overly broad,
or unseen-section references. IDs refer to extracted-text lines, not the printed
transcript line numbers. Existing PDF page markers and transcript line numbers
remain in the retrieved passages. Strict verbatim legacy quotes are still validated
against the source; fuzzy quote matching is not used.

Each generated sentence carries its quotations and source offsets in
`chronology.json` under `deposition_evidence`, alongside the source filename,
transcript hash, excluded prefix count and protocol version. A separate model call
checks every sentence against the evidence and supplied source context, including
negations, attribution and qualifications. One summary repair is allowed. An
unsupported or uncertain result after repair stops generation; rerunning does not
repeat a negative review until it happens to pass. This model review adds a check
but does not certify medical or legal accuracy. Review the original Q/A and PDF.

Long transcripts are read in bounded slices before synthesis. If the collected
evidence cannot fit a complete synthesis request, generation stops rather than
silently omitting the remainder. Verification sends every slice of candidate
transcripts whose covers match the deposition date. Findings from partial source
groups still require human reconciliation. No additional verification service is
required; this extends the recursive app's existing verification step.

## Checkpoints and review details

Identity, each long-transcript section, synthesis and support reviews are saved
separately. A transient interruption resumes from completed stages. Invalid JSON
or evidence references receive one corrective attempt; repeated failures stop.
Private `batch_NNN.deposition-work.json` files retain rejected responses, review
findings and the blocked stage. Failed-session screens offer a download for source
review. These files are restricted to the server owner, stay in the session batches
folder, and are not added to output uploads or the completed-output ZIP. They may
contain sensitive source passages and should be handled as case material.

Runs with the current `same-day-care-v3` batch manifest can resume after this
update. Completed clinical batches are retained; completed deposition batches
without evidence protocol 2 are rechecked when generation resumes. Stage caches
are keyed to source text, filename, model and evidence protocol. Different batch
sources/model or older batching rules still require a new run. A run already at
assembly or verification is not retroactively rewritten.

Current limitations: detection requires readable Q/A structure and a recognizable
cover/boundary. Unusual OCR layouts may need a transcript-only copy or corrected
OCR. Multi-session or multi-witness bundles should be split by session/witness;
ambiguous metadata is sent for review. The first 20,000 transcript characters must
contain a supported cover date and witness name. See [extraction coverage](extraction-coverage.md) for OCR failures and page review.
Validation uses synthetic tests
with mocked model responses; provider-backed behavior still needs a staged run.
