# Jev API pilot

Jev adds a second, optional reviewer to the current medical case workspace. It runs during generation/export, after final therapy consolidation, using each entry's complete cited OCR pages. It never replaces generation, the existing evidence validator, the full-page coverage audit, or human review. Historical Streamlit runs retain their original behavior.

## Private configuration

The feature is off by default. Adding a key alone does not enable patient-data transmission.

1. Create a TypeSafe API key privately in your account. Do not paste it into chat, source code, screenshots, or an issue.
2. For the local synthetic benchmark, save it as `TYPESAFE_API_KEY=...` in an ignored `.env.local` file with owner-only permissions. Alternatively use your existing secret manager/environment.
3. After benchmark and data-handling approval, configure the existing Render service with `TYPESAFE_API_KEY`, `JEV_MODEL=jev-1.13.0`, and `JEV_ENABLED=true`. Enable only on an isolated synthetic deployment initially. The flag applies to every new-workspace job on that service, including Regenerate exports; it is not a per-case permission.
4. Enabling sends final narrative entry text and the complete cited pages (potentially containing patient identifiers) to `https://api.typesafe.ai/v1/systemone`. Confirm the applicable TypeSafe agreement, retention settings and any required BAA before patient use. No patient case was sent as part of implementation testing.

The client uses the existing HTTP dependency; no TypeScript rewrite or new runtime is required. It pins the model, rejects redirects and unexpected model versions, limits retry attempts, and redacts remote error bodies and keys. A missing key, malformed result, oversized complete evidence, missing source, or changed original/extraction is visibly incomplete. A provider failure stops new requests for that export; resume retries unfinished work. Up to 500 new API requests (including diagnostic calls) are made per export; successful cached entries do not use this allowance.

## Synthetic API acceptance

From the repository, after installing `requirements-dev.txt`:

```sh
python scripts/jev_benchmark.py
python scripts/jev_benchmark.py --live --env-file .env.local --output /tmp/jev-synthetic-benchmark.json
```

These commands retain the legacy six-question benchmark for comparisons; they do not reproduce the v4 two-stage execution. The first command validates fixtures without calling the API. The live command sends only 18 built-in fictional examples. It measures the overall factual-support label, saves all six decision dimensions, and counts false accepts. It stops on a provider error and retains completed results. Re-running the benchmark makes fresh calls; unlike production review, it deliberately does not replay cached model responses. This small challenge set is engineering validation, not a medical accuracy estimate or a calibrated decision threshold. Use a larger independently labeled, held-out set to evaluate missed errors and false flags before relying on the feature.

The earlier manual playground smoke test returned the expected labels on six short examples using `jev-1.13.0`. That used a simpler single-question prompt and does not validate this six-dimension API integration. Local mocked HTTP tests validate implementation behavior, not Jev's judgment.

## Live results: September 19, 2026

The saved local key authenticated successfully against the pinned model. Production remains disabled. Only the fixed fictional fixtures were transmitted.

| Run | Expected overall labels | Unsupported claims labeled supported | Correct entries flagged | Total entries flagged |
| --- | --- | --- | --- | --- |
| Baseline v1 | 16/18 | 0/13 | 5/5 | 18/18 |
| Revised v2 | 17/18 | 0/13 | 5/5 | 18/18 |

The fixtures contain **five** supported claims and thirteen unsupported claims. Earlier conversational reporting counted six supported claims incorrectly; the saved fixtures and these counts are authoritative. Raw sanitized responses are preserved in [baseline-v1.json](jev-evaluation/baseline-v1.json) and [revised-v2.json](jev-evaluation/revised-v2.json). Baseline prompts are in commit `ab74eff87f494b306612dd04a4e63cb7f0a25a7a`; v2 prompts are retained in commit `a3e12af0f339b909f13ee2033d2afb1a2f020f1a`. The v2 revision separates dimension scope and distinguishes absent evidence from contradiction. It fixes the medication-label mismatch. The signature-date claim still receives insufficient evidence instead of contradiction.

**The v1/v2 pilot failed its review-workload acceptance check:** every correct entry still receives a flag, mostly because at least one dimension falls below the unchanged 0.90 threshold. Zero false accepts on thirteen examples does not establish reliable error detection; flagging everything is not useful discrimination. Prompts were revised against the same fixtures, so v2 is a development result, not held-out validation. All six returned decisions are retained, but only the overall label has a manually specified label for each fixture. The legacy benchmark reports v3 routing alongside label accuracy using the shared routing rule.

Keep the feature disabled for team use. Before activation, evaluate a separately labeled set of realistic, longer fictional records with per-dimension labels, establish acceptable missed-error and false-flag limits, and validate any prompt or threshold change on held-out examples. Test a synthetic case through the deployed workspace and confirm provider data arrangements before patient use. No live deployed-workspace or patient-data acceptance was completed here. Local mocked end-to-end tests cover export reports, visible issues, caching and unchanged prior versions.

## Follow-up evaluation and local workflow

V3 makes the supported/not-applicable choices mutually exclusive: supported requires an actual assertion in the dimension. The unchanged 0.90 routing rule still flags 5/5 correct development examples; overall labels remain 17/18. This clarification does not by itself solve the workload problem. The [v3 questions](jev-evaluation/questions-v3.json) and [development responses](jev-evaluation/clarified-v3.json) are retained.

Before making holdout API calls, six longer fictional records with paired correct/incorrect claims were authored in `scripts/jev_acceptance_fixtures.py`. Labels for all six dimensions and an engineering gate were specified in that file first: zero unsupported claims escaping review and no more than one of six correct entries flagged. These labels were authored by the coding assistant from the supplied text, not by independent clinicians. They were not derived from Jev responses. The prompts and threshold were not tuned after seeing these results.

[Holdout results](jev-evaluation/holdout-v3.json): **12/12 overall labels**, **67/72 dimension labels**, **0/6 unsupported claims escaping review**, and **4/6 correct entries flagged**. The engineering gate failed. Two correct entries received no flags; that is not medical clearance. The benchmark exits with a failure code when the workload gate fails even if every overall label matches. This small set is independent of prompt tuning, but is not independent clinical validation or a statistically reliable sensitivity estimate.

The [local workspace smoke result](jev-evaluation/workspace-v3.json) used a newly generated fictional PDF, actual PDF text extraction and a real Jev call. It verified case details, visible review-issue data, source PDF/text/PNG preview routes, authenticated downloads, rejection of unauthenticated access, unchanged narratives and historical exports, and a repeat export without a second Jev call. Generation and cloud upload used deterministic fixtures; neither Claude nor Dropbox was called. This was a local route-level test, not a rendered browser or deployed Render test. A subsequent offline replay verified the final review-message wording and a nonempty cache.

```sh
python scripts/jev_benchmark.py --live --suite holdout --env-file .env.local --output /tmp/jev-holdout.json
python scripts/jev_workspace_smoke.py --live --env-file .env.local --output /tmp/jev-workspace.json
```

The smoke script requires development dependencies and Poppler tools. It always creates an isolated temporary fictional workspace and removes it on exit. Its environment changes affect only its process; it never enables the real service or modifies `.env.local`. Re-running live evaluations incurs fresh requests.

Review messages now distinguish a suggested source contradiction, missing source support, and model uncertainty. All three still require review under the same rule. No findings are suppressed, and a low-confidence response is not presented as proof of an error. TypeSafe documents confidence as a statistic derived from its option distribution, not a clinical accuracy probability: [confidence reference](https://docs.typesafe.ai/confidence).

**V3 decision (superseded by the v4 code-review decision below): retain draft PR and keep production disabled.** The integration works locally, but the promised workload gate has not passed. Next evaluation should compare a separate simpler question design on development data, then use a fresh holdout; do not keep tuning on this now-observed set. A rendered browser test and isolated Render acceptance remain required before activation. The 523-test regression suite passes with four existing manual/credential-dependent skips.

## V4: overall support first, diagnostics when needed

A fresh paired comparison used twenty fictional examples in `scripts/jev_triage_fixtures.py`, frozen before API calls. Each candidate request contained only the unchanged v3 overall support question; each baseline request contained all six frozen v3 questions. The threshold stayed at 0.90. The predeclared engineering gate required zero unsupported claims escaping review and at most one of ten correct claims flagged. Labels were authored by the coding assistant from the source text, not independently by clinicians.

| Approach | Overall labels correct | Unsupported claims escaping review | Correct claims flagged |
| --- | --- | --- | --- |
| Single overall gate | 20/20 | 0/10 | 0/10 |
| Six-question baseline | 19/20 | 0/10 | 10/10 |

The candidate passed this small engineering gate. Full requests, responses, frozen schemas, fixture hash and the predeclared gate are saved in [triage-comparison-v4.json](jev-evaluation/triage-comparison-v4.json). No prompt or threshold tuning followed these results. This does not prove clinical sensitivity or rule out errors on longer entries; the overall gate can miss a detail that an independently run diagnostic would catch.

```sh
python scripts/jev_compare_triage.py --live --env-file .env.local --output /tmp/jev-triage-comparison.json
python scripts/jev_workspace_smoke.py --live --env-file .env.local --output /tmp/jev-workspace-v4.json
```

The [v4 workspace smoke](jev-evaluation/workspace-v4.json) exercised the real primary and diagnostic API stages, immutable history, cached repeats, source previews and authenticated downloads. The complete supported chronology paragraph was flagged because primary confidence was **0.61**. Its label was supported, but it failed the unchanged threshold; diagnostics remained visible. This full-entry warning is reported separately from the focused-claim comparison and must not be omitted when judging workload.

The [local rendered-browser checks](jev-evaluation/browser-v4.md) passed sign-in, chronology/source presentation, review messages, original-page display and current/previous export listings. Source text extraction was real; generation and cloud delivery were fixtures. No patient records were sent and no Render deployment occurred.

Final regression: **527 passed, four existing manual/credential-dependent skips**. Separate tests verify conditional diagnostics, failure retention, stage-specific retry and shared request limits.

**Code-review decision:** the two-stage implementation is ready for review as a disabled experimental feature. Production remains disabled. Activation still requires a larger independently labeled set of full formatted entries, agreement on acceptable error and warning rates, and isolated Render acceptance including real generation/OCR, storage persistence and delivery. The passing focused-claim comparison does not override the observed full-entry limitation.

## Review behavior and evidence

V4 first asks only the overall factual-support question. Supported at confidence >= 0.90 produces no additional Jev flag, and the five detailed checks are explicitly marked not requested. Any other overall result requires review and triggers the five diagnostic checks: date roles, attribution, negation, anatomy and procedure status. Diagnostics cannot overturn the primary flag. Missing evidence or a low-confidence primary result is never a pass. All returned findings and confidence below 0.90 remain visible as review flags. This threshold is a conservative routing heuristic, not a validated 90% clinical accuracy claim. Even all-supported results remain `human_review_required`.

`jev_review.json` and `jev_review.md` accompany the immutable output version and its manifest. `verification.md` includes the Jev report. The existing Review tab shows affected source documents and pages; flagged paragraphs remain in the draft and are labeled accordingly. Existing source-review actions and named history remain authoritative. Jev does not independently resolve a human decision or change the narrative.

Checkpoints in `medical-work/jev/` key on the final entry, complete cited pages, source revisions, fixed questions, model, threshold and protocol. Changes create new checkpoints and export versions. Old exports stay intact. Disabling Jev reports that it did not run and does not clear previous unresolved Jev flags. Primary and diagnostic stages have separate schema-validated checkpoints. A failed diagnostic stage retains the primary flag, marks the report incomplete, and retries only unfinished work. Both stages share the request budget and provider-failure circuit breaker. Missing/corrupt checkpoints cannot become a pass.

Evidence comes from the recorded case original hash, saved OCR hash and physical page markers, plus separately retained reviewer corrections. A named human transcription without matching OCR is left for human review. Full cited pages are used, not just short quotations; oversized evidence is flagged rather than truncated. Uncited pages and omitted encounters are outside this check. OCR mistakes can still be supported by the OCR text and require original-image review.

## Deployment and rollback

Deploy only after the existing workspace preflight confirms no active jobs/locks. Keep `JEV_ENABLED=false` until the private key and isolated synthetic acceptance are ready. No new Render service or disk is required. Use Regenerate exports to add the Jev reports to an eligible completed case; existing downloads stay unchanged. Use Run / Resume after source changes. To pause future Jev calls set the flag false and restart safely at job boundaries. Retain reports, checkpoints and unresolved findings for review.

Official references, checked September 19, 2026:
- [API contract](https://docs.typesafe.ai/api)
- [Models and context limits](https://docs.typesafe.ai/models)
- [Confidence](https://docs.typesafe.ai/confidence)
- [Known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- [Citation checking](https://docs.typesafe.ai/cookbooks/citation_check)
- [Data policy](https://typesafe.ai/legal/privacy-policy)
