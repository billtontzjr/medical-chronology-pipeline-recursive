# Team release — September 8, 2026

This release addresses the September 7 readiness findings. Code tests do not certify patient chronology accuracy or prove the production deployment is ready.

## Render setup

1. In the existing service's Environment page, set `TEAM_PASSWORD` to a private password of at least 16 characters. Share it with authorized team members through your password manager. Do not commit it or paste it into a task.
2. Confirm the existing persistent disk is mounted at `/app/data`. Keep `SESSION_DATA_DIR=/app/data` and `REQUIRE_PERSISTENT_STORAGE=true`. The Dockerfile defaults to these values; startup refuses to load cases if the disk is not mounted.
3. Keep the existing Dropbox OAuth, Google Vision, Anthropic and OpenAI credentials. Set `OPENAI_API_KEY` if using an OpenAI model.
4. Use the repository Dockerfile. The start command is now `python serve.py`. Remove any Render Docker-command override that starts Streamlit directly. The gateway binds to Render's `PORT`; Streamlit runs privately on loopback port 8502.
5. Set the health-check path to `/healthz`. This is a liveness probe, not a certificate that credentials or case processing work. Without a configured team password, patient routes remain closed and display a setup message.

The gateway protects all routes, including downloadable files and WebSockets. Cookies are Secure, HttpOnly, SameSite=Strict, expire after eight hours, and are invalidated by a password change or server restart. Sign-out revokes the current token and closes its live connection. There is a process-wide sign-in throttle. A shared team password grants access to all cases; this release does not provide individual accounts or per-case permissions.

## Existing cases and source protection

Older drafts remain available after sign-in. Use **Start refreshed run** on a legacy case to create a separate run that downloads and extracts the original source again. The old draft is preserved. A resumed legacy run with no confirmed source inventory is blocked rather than declared complete.

New downloads first inventory every nested folder and listing page. File checkpoints are reused only when the remote size and content hash match. A second inventory check detects source changes during download. OCR tracks source/text fingerprints and page results. Failed or missing pages block generation; pages returning no text remain flagged for human review.

Uploads are restricted to a case folder below `/Medical chronology pipeline outputs`. A different existing file is never overwritten. For revised outputs, choose a new case output folder. Original Dropbox records are only read. **Archive** moves a local case into `/app/data/archive`; it preserves files and Dropbox outputs. An administrator can restore an archived case by moving its complete folder back to `/app/data/sessions` while the app is stopped.

Verification saves each completed source-group result and resumes unchanged inputs. Changes to the model, draft or source invalidate reuse. It remains an AI-assisted review, and unresolved source sections remain on the manual-review worksheet. Word output is left-aligned with zero paragraph indents and 12-point paragraph spacing. Billing appendix export uses saved record types, with explicit-label support for older drafts.

## Release acceptance

- Open a fresh browser session: patient content and a copied direct download link must require sign-in.
- Sign in, open a saved case, download Word and ZIP, sign out, and confirm the copied download URL is denied.
- Start a refreshed test case; reconcile the downloaded file count and OCR page totals with the original source inventory.
- Finish generation and verification; review every OCR and manual-review flag against the original records.
- Confirm the saved case remains after a service restart and review the Render logs for processing errors.

Do not approve routine team use until these live checks pass. Local tests use synthetic source material and mocked external processing, never patient records.

Render references: [environment settings](https://render.com/docs/configure-environment-variables), [persistent disks](https://render.com/docs/disks), [health checks](https://render.com/docs/health-checks).
