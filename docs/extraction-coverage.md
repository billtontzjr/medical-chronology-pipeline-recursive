# Extraction coverage and interrupted runs

OCR now records every expected PDF page as text, no text, or extraction error.
The PDF page count must be established; the app no longer substitutes an assumed
100-page limit. An error converting one expected page does not silently discard
all later pages.

Pages with no recognized text may be blank, image-only or unreadable. They trigger
a page-review warning, not an assertion that the document is complete or that the
page is blank. Technical failures or missing results block chronology generation.
Resume retries affected PDFs and preserves completed files. Each PDF's text is
written atomically and checkpointed immediately, before processing the next PDF.
A pending coverage marker prevents interrupted new extractions being mistaken for
complete legacy text.

The current-session and Sessions views offer a page coverage report when review
is needed. Completed output packages include `ocr_coverage.json`. The report lists
source filenames, page totals, pages returning text, pages returning no text and
failed pages. Extraction coverage is separate from medical accuracy and from the
app's later source verification.

Older runs did not save page totals or reasons for missing text. They retain their
existing extracted files and show unknown coverage rather than invented totals or
a forced, potentially expensive OCR rerun. Review the original pages to resolve
those unknowns. Source PDFs are never edited by this workflow.

Generation progress names the configured model. A failed run also marks the active
phase failed instead of leaving it displayed as running. Deposition stages show
which checkpoint is being processed or reused; see [deposition summaries](deposition-summaries.md).
