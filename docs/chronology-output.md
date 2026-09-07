# Consolidated chronology and Word downloads

New runs save chronology.docx alongside Markdown, JSON, summary and gaps. The
completed-session screen has a prominent Word download, and Download all includes
the binary Word file. Older saved sessions with chronology.md can generate Word
on demand without another model call. Export uses Times New Roman 12-point text,
dated paragraphs, a centered patient header and page numbers. The default Word
document has the same dated entries as Markdown; the optional billing appendix
only moves entries already explicitly labeled billing-only.

The literal `Visit Type:` label is removed during assembly and Word conversion;
the service description remains. Clinical generation requests one paragraph per
date of service, consolidating all care by that provider on that date. Routine
therapy must not be grouped across multiple dates in new runs.

## Consolidation across batches

After all batches are collected, identical entries are removed and affirmative
same-date, named-provider matches are consolidated into one paragraph. This stage
can combine office evaluation, cervical procedures and lumbar procedures that
were extracted into different batches. Provider credential punctuation is
normalized. A surname-only reference joins a full name only when the match is
unique on that date. Different named providers and deposition summaries stay
separate. Unknown identities are not guessed or merged merely by date.

The configured model must account for every input and preserve distinct clinical
findings, procedures, levels, laterality, medication details, plans and conflicts.
Clinical notes supersede an overlapping billing-only assertion that no note
exists. Incomplete input coverage, a changed date or a changed provider stops
assembly for review. The input paragraphs, result and coverage explanations are
saved in chronology.json under encounter_consolidation. A content/model signature
allows interrupted assembly to reuse completed consolidation.

Coverage validation establishes that every input was accounted for; it does not
prove that every clinical fact survived paraphrasing. Source verification and
review remain necessary, especially for unreadable provider names, name variants
that cannot be matched confidently, and billing attributed to a different entity.
Automated regression tests use synthetic records and mocked model responses; a
new model-backed case run is still needed after deployment.

Use a fresh run after deployment. Older batch manifests are rejected instead of
quietly reusing the previous generation rules. Word export of an old completed
session formats its existing draft; it does not retroactively deduplicate that
draft or apply new source exclusions.
