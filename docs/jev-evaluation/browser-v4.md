# Local browser acceptance — v4

Tested through the in-app browser against an isolated fictional workspace on loopback.

- Team sign-in succeeded without changing authentication protections.
- Overview displayed the fictional Alex Example case and completed processing stages.
- Review displayed a Jev uncertainty message, the retained draft paragraph, its physical page reference and original PDF preview. Expanded paragraph and original page were visually inspected; no layout overlap was observed at the default viewport.
- Chronology displayed the dated paragraph and source-page controls.
- Exports displayed current Word, JSON and Markdown artifacts, both Jev reports, and previous-version downloads. Actual authenticated downloads were verified by the companion route test.
- No review decision was recorded, no patient case was loaded, and no production service was changed.

The complete paragraph was labeled supported with confidence 0.61, causing a retained review flag. The diagnostic stage ran successfully. This is an observed workload limitation, not a successful full-entry judgment acceptance.

Cloud upload and generation were deterministic fixtures. Browser acceptance here covers the local interface only, not Render configuration, persistence, external OCR/generation or Dropbox delivery.
