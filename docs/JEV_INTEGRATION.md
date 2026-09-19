# Jev API pilot

Jev adds a second, optional reviewer to the current medical case workspace. It runs during generation/export, after final therapy consolidation, using each entry's complete cited OCR pages. It never replaces generation, the existing evidence validator, the full-page coverage audit, or human review. Historical Streamlit runs retain their original behavior.

## Private configuration

The feature is off by default. Adding a key alone does not enable patient-data transmission.

1. Create a TypeSafe API key privately in your account. Do not paste it into chat, source code, screenshots, or an issue.
2. For the local synthetic benchmark, save it as `TYPESAFE_API_KEY=...` in an ignored `.env.local` file with owner-only permissions. Alternatively use your existing secret manager/environment.
3. After benchmark and data-handling approval, configure the existing Render service with `TYPESAFE_API_KEY`, `JEV_MODEL=jev-1.13.0`, and `JEV_ENABLED=true`. Enable only on an isolated synthetic deployment initially. The flag applies to every new-workspace job on that service, including Regenerate exports; it is not a per-case permission.
4. Enabling sends final narrative entry text and the complete cited pages (potentially containing patient identifiers) to `https://api.typesafe.ai/v1/systemone`. Confirm the applicable TypeSafe agreement, retention settings and any required BAA before patient use. No patient case was sent as part of implementation testing.

The client uses the existing HTTP dependency; no TypeScript rewrite or new runtime is required. It pins the model, rejects redirects and unexpected model versions, limits retry attempts, and redacts remote error bodies and keys. A missing key, malformed result, oversized complete evidence, missing source, or changed original/extraction is visibly incomplete. A provider failure stops new requests for that export; resume retries unfinished work. Up to 500 new entry requests are made per export; successful cached entries do not use this allowance.

## Synthetic API acceptance

From the repository, after installing `requirements-dev.txt`:

```sh
python scripts/jev_benchmark.py
python scripts/jev_benchmark.py --live --env-file .env.local --output /tmp/jev-synthetic-benchmark.json
```

The first command validates fixtures without calling the API. The live command sends only 18 built-in fictional examples. It measures the overall factual-support label, saves all six decision dimensions, and counts false accepts. It stops on a provider error and retains completed results. Re-running the benchmark makes fresh calls; unlike production review, it deliberately does not replay cached model responses. This small challenge set is engineering validation, not a medical accuracy estimate or a calibrated decision threshold. Use a larger independently labeled, held-out set to evaluate missed errors and false flags before relying on the feature.

The earlier manual playground smoke test returned the expected labels on six short examples using `jev-1.13.0`. That used a simpler single-question prompt and does not validate this six-dimension API integration. Local mocked HTTP tests validate implementation behavior, not Jev's judgment.

## Review behavior and evidence

The six checks cover overall factual support, encounter-date roles, attribution, negation, anatomy and procedure status. Nonapplicable dimensions are explicit; missing evidence is not a pass. All findings and confidence below 0.90 produce review flags. This threshold is a conservative routing heuristic, not a validated 90% clinical accuracy claim. Even all-supported results remain `human_review_required`.

`jev_review.json` and `jev_review.md` accompany the immutable output version and its manifest. `verification.md` includes the Jev report. The existing Review tab shows affected source documents and pages; flagged paragraphs remain in the draft and are labeled accordingly. Existing source-review actions and named history remain authoritative. Jev does not independently resolve a human decision or change the narrative.

Checkpoints in `medical-work/jev/` key on the final entry, complete cited pages, source revisions, fixed questions, model, threshold and protocol. Changes create new checkpoints and export versions. Old exports stay intact. Disabling Jev reports that it did not run and does not clear previous unresolved Jev flags. Missing/corrupt checkpoints cannot become a pass.

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
