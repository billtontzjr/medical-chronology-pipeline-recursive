# precision-chronology

Anti-hallucination medical-legal chronology generation from PDF medical records.

Successor to `medical-chronology-pipeline-recursive`. The user-facing workflow is unchanged — Dropbox in, Dropbox out, Streamlit UI on Render — but the internal pipeline has been redesigned so that **a clinical claim cannot appear in the final chronology without a literal source quote backing it**.

## Why this rebuild exists

The predecessor used a single-pass generation prompt: Claude read a batch of records and wrote narrative entries directly. There was no requirement that the generated text be grounded in verbatim source quotes, and the verifier that existed in the codebase was never wired into the pipeline. On a recent run this produced a hallucinated record — a medical-legal failure mode that justified a ground-up rebuild of the middle phases.

In this app, every chronology entry is derived from atomic facts that each carry a verbatim quote from the OCR source. A deterministic Python verifier confirms each quote appears as a literal substring of the source before any narrative is generated. The narrative assembly step never sees the raw records — only the verified facts. This makes it structurally impossible for a clinical claim to appear in the final chronology without a literal source quote backing it.

## Architecture: eight phases

```
                  (Dropbox link)
                       |
            1. download        DropboxTool.get_shared_link_files
                       |
            2. ocr             OCRClient + Google Vision (page markers)
                       |
            3. extraction      Claude → atomic facts with verbatim quotes  [chunk-level resume]
                       |
            4. verification    Pure Python substring + date normalization  [LOAD-BEARING]
                       |
            5. assembly        Claude → narrative entries (sees only verified facts)
                       |
            6. cross_check     Token-set Jaccard sanity report
                       |
            7. header_summary  Patient header + summary + gaps + JSON + DOCX
                       |
            8. upload          DropboxTool.upload_folder
                       v
                  (Dropbox folder)
```

Phases 1, 2, 8 are ported from the predecessor (Dropbox / Google Vision integrations work fine). Phases 3 through 7 are net new and constitute the anti-hallucination core.

Each phase persists its artifacts to `data/sessions/<session_id>/...`. Status is tracked in `state.json`, so the pipeline can be paused (via the `PAUSE` marker file) or killed and resumed at the next phase boundary. The extraction phase is additionally resumable at chunk granularity — each chunk's output JSON is written atomically.

## Hallucination Prevention Guarantees

What this app **does** protect against:

* Fabricated clinical content. The model cannot insert findings into the chronology that did not survive substring verification.
* Wrong-visit attribution. Facts carry their own `visit_date` / `facility` / `provider_name` from the quote, and the assembler groups by that identity before narrating.
* Invented dates. `visit_date` is set to `null` unless the quote itself contains a date, and is normalized to MM/DD/YYYY by the verifier.

What this app **does not** protect against:

* OCR errors in the source. If the original PDF reads "L4-L5" as "L4 LS", the verifier accepts the garbled quote because it matches the OCR text, and the chronology repeats the OCR error. Garbage in, garbage out.
* Wrong dates already in the source. If the medical record itself lists 03/14/2024 when the actual visit was 03/12/2024, this pipeline cannot detect or correct that.
* False negatives. If the model fails to extract a fact that is actually present in the source, the chronology will have a gap. This is failure-by-omission, not hallucination. The gaps analysis flags suspicious gaps for human review.

## Repository layout

```
precision-chronology/
├── README.md
├── Dockerfile                    # python:3.11-slim, runs `streamlit run`
├── render.yaml
├── requirements.txt
├── .env.example                  # copy to .env locally
├── app.py                        # Streamlit UI entry point
├── run_pipeline.py               # CLI entry point (Click)
├── src/
│   ├── pipeline.py               # PrecisionChronologyPipeline orchestrator
│   ├── session_state.py
│   ├── ocr_client.py             # OCR with === PAGE N === markers
│   ├── extractor.py              # chunking + Claude extraction
│   ├── verifier.py               # the load-bearing component
│   ├── assembler.py              # grouping + Claude assembly
│   ├── cross_checker.py          # token-set Jaccard sanity pass
│   ├── header_extractor.py
│   ├── summary_generator.py
│   ├── renderers.py              # chronology.json + chronology.docx
│   ├── anthropic_client.py
│   ├── schemas.py                # Pydantic models for facts
│   ├── prompts/                  # extraction, assembly, header, summary
│   └── tools/dropbox_*.py        # ported unchanged from predecessor
├── tests/
│   ├── test_schemas.py
│   ├── test_verifier.py          # 100% coverage on the load-bearing module
│   ├── test_cross_checker.py
│   ├── test_pipeline.py          # full end-to-end with mocked externals
│   └── fixtures/sample_records.txt
└── data/sessions/                # created at runtime, gitignored
```

## Local development setup

Requires Python 3.11 (the Dockerfile target). Local development on 3.9+ works for unit tests; integration tests run with the same dependencies.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in:
#   ANTHROPIC_API_KEY
#   GOOGLE_VISION_API_KEY
#   DROPBOX_APP_KEY
#   DROPBOX_APP_SECRET
#   DROPBOX_REFRESH_TOKEN
```

To run the unit + integration tests (no external services required):

```bash
pytest -q
# 54 tests, including a full pipeline integration run with mocked externals
```

To run the verifier with the strict coverage gate:

```bash
pytest --cov=src.verifier --cov-fail-under=95 tests/test_verifier.py
```

To run a chronology through the CLI:

```bash
python run_pipeline.py \
    --dropbox-link "https://www.dropbox.com/scl/fo/.../..." \
    --patient-id DOE_JANE \
    --destination "/Precision chronology pipeline outputs/DOE_JANE"
```

To run a chronology through the UI:

```bash
streamlit run app.py
```

## Render deployment

The repository ships a `render.yaml` describing a Render Web Service that builds from the Dockerfile and serves Streamlit. Setup is manual after the initial push:

1. Push this repo to GitHub.
2. In the Render dashboard, **New** → **Web Service**, connect the repo. Render reads `render.yaml` for the service definition.
3. On the new service, paste the five environment variables (`ANTHROPIC_API_KEY`, `GOOGLE_VISION_API_KEY`, `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`). `ANTHROPIC_MODEL` is optional and defaults to `claude-sonnet-4-6`.
4. (Optional) Attach a 1 GB persistent disk at `/app/data/sessions` to keep in-flight sessions resumable across deploys (~$1/month). Without it, sessions in flight at deploy time are lost; completed sessions are already uploaded to Dropbox so the loss is bounded.
5. The first deploy takes ~3 minutes (Docker image build). Subsequent deploys cache the dependency layer.

## Comparison vs. predecessor

| Concern | predecessor | precision-chronology |
| --- | --- | --- |
| Generation strategy | Single-pass: Claude reads records, writes entries directly. | Atomic facts with verbatim quotes; assembler narrates only over verified facts. |
| Verifier | Exists in the codebase, never called by the pipeline. | Pure-Python substring search wired in as Phase 4; **load-bearing**. |
| Chunking | Batch granularity (multiple records per Claude call). | ~8000-char chunks with 800-char overlap; one Claude call per chunk; chunk-level resume. |
| Cross-check | None. | Phase 6 token-set Jaccard report; soft by default, `--strict-cross-check` available. |
| Hallucination signal to user | None. | Verification Report panel: verified/rejected counts, rejection-reason breakdown, downloadable JSONLs. |
| Date normalization | Implicit / inconsistent. | Verifier normalizes every accepted fact to MM/DD/YYYY; non-parseable dates flag the fact as low-confidence. |
| Default model | claude-sonnet-4-6 | claude-sonnet-4-6 (selectable per stage; Opus 4.7 available). |

## Open decisions / configurable defaults

| Knob | Default | Where to change |
| --- | --- | --- |
| Extraction model | `claude-sonnet-4-6` | UI selectbox; `--model-extraction` CLI flag; `ANTHROPIC_MODEL` env var. |
| Assembly model | `claude-sonnet-4-6` | UI selectbox; `--model-assembly` CLI flag. |
| Chunk size | 8000 chars | `CHUNK_SIZE` in `src/extractor.py`. |
| Chunk overlap | 800 chars | `CHUNK_OVERLAP` in `src/extractor.py`. |
| Cross-check threshold | 0.35 | `CROSS_CHECK_JACCARD_THRESHOLD` in `src/cross_checker.py`; `--cross-check-threshold` CLI flag. |
| Strict cross-check | off | UI checkbox; `--strict-cross-check` CLI flag. |
| Render disk | not attached | Uncomment the `disk:` block in `render.yaml`. |

## Notes for operators

* The temperature for every Claude call in this codebase is **zero**. There is no knob for this and it is intentional — non-zero temperature is incompatible with verifier-driven workflows.
* The DOCX renderer uses Times New Roman 12pt, single-spaced, justified, with the header block in plain (non-bold) prose. This matches Dr. Tontz's standing document format preferences.
* The team (Clare, Cylee, Gaby) uses the Streamlit UI exclusively; the CLI is provided for batch automation and CI.
