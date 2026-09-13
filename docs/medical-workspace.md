# Medical case workspace

New medical-only cases use the authenticated `/workspace` UI and a supervised worker. Closing a browser does not cancel a job. Existing runs remain available with their original scope and model through `/?legacy=1&session_id=...`; they are never converted automatically.

## Scope and output

The default includes clinical care, procedures, imaging, therapy and attributed substantive IME/medical expert evaluations. It excludes testimony (including AI deposition summaries), legal correspondence, pleadings, releases, liens, fee agreements, administrative material, cost-only projections and billing-only records. Case settings can explicitly include billing or exclude expert evaluations. The policy is frozen at creation. Actual clinical attachments remain eligible even inside legal bundles. Original source PDFs are never changed or deleted.

The source pane renders the selected physical PDF page with Poppler, behind the same authentication as the case. Rendering uses a private temporary snapshot checked against the recorded source hash; it never rewrites the original. Page requests have bounded resolution, timeouts and concurrency. The team can enlarge a page or open the original PDF; rendering failures show a clear fallback.

Each accepted entry retains its document ID, original SHA-256, physical PDF page, exact source excerpt and extraction revision. Generation and a separate full-page coverage check must support it. A material question withholds the affected source group and leaves a review item. Exact binary duplicates retain their original paths. Text-identical or similar records with matching service-date fields require comparison before the second source is included. Same-day clinical entries for an affirmative provider identity are consolidated with all procedures and citations; diagnostic reports and different providers remain separate. Grouped therapy entries must retain evidence for every listed date.

Word uses dated narrative paragraphs, 12pt Times New Roman, Letter pages and 1-inch margins, with a centered patient/DOB/DOI heading and page footer. The underlined profile follows the reference title and justified body pattern; classic preserves the existing left-aligned body. Plain uses an unbolded, left-aligned heading and body for the other reference pattern. Paragraph length follows the clinical content. There is no `Visit Type:` label or chronology table. Source links live in the workspace and JSON rather than cluttering the narrative Word output.

## Review and completion

Processing, review and Dropbox delivery have separate states. `Complete` means the job produced and delivered its available outputs. It does not mean human approval. Unresolved source material is withheld; the exported chronology states `Draft complete—manual review required`, and `manual_review.md`, `.docx` and `.json` identify the document, pages and question. An optional companion-report failure preserves the detailed chronology, marks the companion phase failed and retains a review flag. A clear unavailable notice replaces that companion; it is never represented as a completed clinical summary.

Review decisions require a name, reason and current source fingerprint. Defer keeps the item flagged. OCR correction saves separately from the original extraction. Identity confirmation applies only to the reviewed section pages. Duplicate comparison permits confirming the duplicate or keeping the source distinct. Reconsideration is a request to reassess original content under the saved scope, never an instruction to include unsupported material. Undo creates a new decision and reopens processing. Editing an exported worksheet does not resolve a saved issue.

After a document decision, **Run / Resume** rebuilds that document and dependent exports. Unrelated document results remain cached. Export-only jobs reject pending source revisions and changed original inventories. Every export version includes a manifest; prior files remain available and are labeled previous versions. Delivery succeeds only when every expected file is confirmed uploaded with content verification.

## Storage and recovery

- Existing `sessions/` and `archive/` remain untouched by the new worker.
- New cases use `medical-sessions/` and `medical-archive/` under `SESSION_DATA_DIR`.
- `workspace.sqlite3` uses WAL transactions and schema version 1 for case indexes, documents, entries, issues, immutable decisions and jobs. A newer schema is rejected without downgrading it.
- `medical-work/` stores source/model/prompt/policy-keyed checkpoints, including rejected responses. One structural-format correction is allowed. Unchanged substantive uncertainty is never retried until the relevant source/review revision changes.
- Case file locks prevent concurrent writers. Jobs retain worker heartbeats; stale leases are recovered after 120 seconds. A live lock remains authoritative. The gateway supervises the worker process, which checks pause/shutdown at saved boundaries.
- Archive/restore preserves the complete tree and decision IDs. Active jobs cannot be archived. Corrupt states and identifier collisions remain visible for recovery.

This is a single-host design on the existing persistent disk. Multiple replicas must not be enabled without replacing local locking and SQLite with a shared coordination design.

## Rollout and rollback

The release is opt-in: `/workspace` is available behind team authentication; `CASE_WORKSPACE_ENABLED=true` makes it the default homepage and routes new-case creation away from the historical Streamlit form. Old `?session_id=` links continue to open the legacy pipeline. The existing password gateway, secure cookie, origin checks and protected source/download routes remain required. Never expose Streamlit's internal port directly.

Before a deployment, run `python scripts/workspace_preflight.py /app/data` on the current service and inspect the worker status. It is a point-in-time check, not authorization to stop a worker. Do not deploy if any case lock is held or any job is queued/running. Back up the persistent disk or use a consistent SQLite backup plus the unchanged session trees. Confirm free disk capacity. Keep recurring deployment automation paused until release acceptance is complete.

Start with the flag disabled and a fictional/source-reviewed isolated case. Verify login, create/run, browser-close recovery, source PDF/OCR access, a named targeted review, duplicate comparison, Word/JSON agreement, archive/restore, and full Dropbox upload verification. Only then enable the default workspace. No model response or passing test suite alone is proof of clinical accuracy; compare a real benchmark against its original records before casewide adoption.

Rollback: stop new jobs at safe checkpoints, back up current data, deploy the prior application and leave `medical-sessions`, `medical-archive` and `workspace.sqlite3` in place. The prior application sees only the original legacy directories. Reinstall the matching workspace release to resume new cases; do not copy new case trees into legacy `sessions` or edit their policy/model files.

## Local acceptance

Run `python -m pytest -q` using the repository environment. Fixtures are fictional and do not contact Dropbox/model providers. Tests cover page evidence, identity, date roles, grouped attendance, source exclusions, same-day care, cached restart, upload completeness, companion failure, path/auth boundaries, targeted decisions, storage isolation and archive/restore.

For a read-only visual preview, run `python scripts/seed_workspace_preview.py /tmp/medical-preview` in an environment with ReportLab, then `python preview_workspace.py --data-dir /tmp/medical-preview --port 8765`. Use an empty fixture directory. The preview binds only to loopback and disables mutations and external calls. Its files are fictional examples, not generated clinical findings.

Physical-page navigation and exact excerpts are available. OCR coordinate highlighting is not implemented because current extraction does not retain word boxes. The reference-format patterns have local structural and synthetic render checks. A real generated chronology still requires source-reviewed benchmark acceptance; synthetic checks are not a replacement for that gate.

## Team review choices

The primary **What should happen next?** dropdown offers **Rerun it and include it in the chronology** and **Exclude it**. These choices apply to the entire selected source document, including all of its entries and related flags. Reviewer name is required; notes are optional. Save multiple decisions, then use Run / Resume once to rebuild the chronology and exports. Existing downloaded versions are immutable.

Exclusion preserves the original, prior results, source hash, reviewer and decision history, and lists the document in the excluded inventory. It bypasses generation for that exact excluded source version. Rerun removes that exclusion and repeats source checks with a new document revision; cached OCR is reused unless the reviewed issue is an OCR failure. Inclusion is conditional on passing evidence checks. A later source change invalidates the decision. Additional correction, deferral and undo tools remain collapsed under More review tools.

The new-case model dropdown offers Anthropic Claude Opus 5, Anthropic Claude Fable 5.1 and OpenAI GPT-6 Astra. Opus 5 is labeled the tested starting point; there is no claim that Fable or Astra outperforms it on these case benchmarks. Provider adapters retain their existing Responses/Anthropic routing and saved-case model protection. Model documentation: https://platform.claude.com/docs/en/models/fable-5-1/overview and https://developers.openai.com/api/docs/models/gpt-6-astra.

Identity parsing reads only the patient-name field. A blank Patient label does not consume an address on the next line; adjacent policy/service-date columns are excluded from the name. Wrong patient names and conflicting DOB evidence remain review failures. Evidence protocol v5 preserves earlier results instead of treating them as reverified.

Evidence protocol v6 recognizes explicit birth-date fields printed with full or abbreviated month names, numeric separators, or ISO dates. The year must be explicit and the date valid; a service date, age, or case metadata cannot supply missing birth-date evidence. The same normalization checks conflicting source DOBs.

An exact ancillary signature citation does not disqualify a separately supported visit date, but signatures alone never establish an encounter date. Every service date still needs a supported source field. Treating and co-signing provider fields may be joined with semicolons only when every component appears in the source, retaining the role wording. All accepted candidates still receive the independent clinical audit. Prior results and exports remain unchanged until a resumed run creates new checkpoints and output versions.

Case badges say **Draft · review required** when processing has finished with open questions, and **Draft generated** otherwise. This separates worker completion from chronology completeness and human sign-off.
