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

Each generated sentence carries matching transcript quotations in
`chronology.json` under `deposition_evidence`, including its source filename,
transcript hash, and excluded prefix character count. These are extracted-text
offsets, not PDF page numbers. Quotations preserve existing page/line markers.
Quote matching establishes provenance, not semantic correctness; review the actual
Q/A and original PDF before relying on the summary.

Long transcripts are read in bounded slices before synthesis. If the collected
evidence cannot fit a complete synthesis request, generation stops rather than
silently omitting the remainder. Verification sends every slice of candidate
transcripts whose covers match the deposition date. Findings from partial source
groups still require human reconciliation. No additional verification service is
required; this extends the recursive app's existing verification step.

Existing saved batches cannot be mixed with this new batching format. Start a new
run after deployment. The app preserves saved results and rejects an incompatible
resume instead of overwriting or reusing old ordinal batch files. An existing run
already at assembly or verification is not retroactively rewritten.

Current limitations: detection requires readable Q/A structure and a recognizable
cover/boundary. Unusual OCR layouts may need a transcript-only copy or corrected
OCR. Multi-session or multi-witness bundles should be split by session/witness;
ambiguous metadata is sent for review. The first 20,000 transcript characters must
contain a supported cover date and witness name. Extraction failures elsewhere in
the upstream OCR pipeline are outside this change. Validation uses synthetic tests
with mocked model responses; provider-backed behavior still needs a staged run.
