# Drafts with manual review

Generation can finish when the model explicitly identifies an uncertain source section and supplies its source ID and review reason. The affected section and every proposed entry citing it are withheld from the dated chronology. Other source sections must still pass the existing source and entry checks. This conservative approach can withhold legible encounters within an uncertain section as well as the unclear material.

The session and exported chronology are labeled **Draft complete—manual review required**. The output folder and ZIP also contain:

- `manual_review.docx`: a team review worksheet with source, section, reported pages, reason, withheld candidate entries, and spaces for reviewer, review date, and resolution.
- `manual_review.md`: the same review instructions and candidates in text form.
- `manual_review.json`: the structured pending items, also included in `chronology.json`.

Page references in this list are reported by the model; confirm them against the original PDF. Candidates are unverified proposals, not approved chronology entries. Review the named sections, confirm dates/provider/treatment, and add only source-supported encounters to the team's reviewed chronology. Keep the reviewed worksheet with that document. This release provides an exportable worksheet; editing it does not automatically update or clear the app's saved flags.

Unresolved items remain prominently included in `gaps.md` and the verification report. An automated report saying no issues were found in the retained draft does not clear the review list or establish that omitted records were reviewed.

Existing blocked source-screening checkpoints can be reused without another model call when they contain a complete, consistent source inventory and explicit review-required dispositions. The original rejection is preserved. Unknown/conflicting source IDs, malformed responses that remain invalid after bounded correction, missing extraction, and deposition evidence failures still stop the run because the app cannot safely partition or publish those results.
