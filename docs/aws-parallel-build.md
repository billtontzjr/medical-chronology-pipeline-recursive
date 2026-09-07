# Parallel AWS chronology workspace

Status: implementation proposal plus isolated reliability fixes; not an AWS deployment or a completed merger of the applications. Prepared September 7, 2026. Preserve the existing Render main branches and services during evaluation.

## Recommended architecture

Use the audit application's Next.js workspace for team review, authenticated source viewing, findings, approvals, and Word export. Port the recursive generator into a Python background worker. Exchange versioned case artifacts rather than calling one app's UI from the other.

| Component | Proposed AWS service | Purpose |
| --- | --- | --- |
| Team login | Cognito | Named users, MFA, no public registration; app enforces case access |
| Web application | ECS on Fargate behind HTTPS load balancer | Next.js review workspace |
| Durable jobs | SQS with dead-letter queue and Fargate worker | OCR, generation, independent review; retry without duplicate outputs |
| Original files and artifacts | Private S3 with versioning and KMS encryption | PDFs, OCR pages, drafts, Word files, versioned evidence |
| Case and job metadata | RDS PostgreSQL | Users, access, statuses, evidence references, review history |
| Model inference | Bedrock | Explicit region/model allowlist and IAM role authentication |
| OCR candidate | Textract | Page-aware OCR; compare with Google Vision before choosing |
| Secrets and operations | Secrets Manager, CloudWatch, CloudTrail | No source text or patient names in routine logs |

Fargate container storage must never be authoritative. Store completed artifacts in S3 and checkpoint metadata in PostgreSQL. The current recursive filesystem store is a transitional single-instance implementation only. Its new required-mount check supports a persistent-volume pilot; it is not the final S3/PostgreSQL implementation. Do not place the audit app's SQLite database on shared EFS as a multi-instance substitute for PostgreSQL.

## Unified workflow and evidence contract

Upload original records once into a case-scoped S3 prefix; create a manifest of object version, SHA-256 hash, file type, and expected page count. Separate medical notes from billing and preserve both. OCR produces one result for every page, including explicit failure or unreadable status. A missing page prevents a complete-coverage designation.

Create an encounter inventory with source file ID, immutable object version, physical PDF page, service-date evidence, date role, provider/facility, and document type. Signature dates, invoice dates, and references to older care must not become encounter dates automatically. Preserve distinct same-day encounters.

Generate a draft from records. Audit it independently against original page text and images, not just the generated summary. Audit both directions: each statement needs source support, and each source encounter needs an included entry or an explicit exclusion decision. Model agreement is not proof. Split individual claims and validate source quotes against the identified page. Unsupported, conflicting, ambiguous, or unreadable evidence stays unresolved.

Every evidence reference must include case ID, file ID, S3 object version, PDF page, text excerpt, and excerpt hash. The source viewer rechecks user/case authorization before issuing a short-lived URL; do not store expiring URLs in the chronology. Clicking a finding opens the cited page. An expired URL should refresh after authentication, not force reprocessing.

Human approval is bound to draft hash, source-manifest hash, OCR version, and review version. Any edit or source change invalidates the relevant approvals. Clean Word output omits citations from its prose, while the separate evidence ledger remains available. Draft exports are visibly distinguished from reviewed exports. Billing-only rows remain in a separate reconciliation view and optional appendix.

## Model and OCR decisions

AWS lists Claude Fable 5.1 in Bedrock. Confirm the actual model identifier, endpoint, region availability, account access, and retention configuration before implementing its adapter. Do not assume every model supports the same Bedrock endpoint or Converse API. The AWS OpenAI model catalog checked for this proposal did not confirm GPT-6 Astra. An all-AWS pilot should use models actually available in that account. Keeping direct OpenAI Astra is a separate data-processing route; the AWS BAA does not establish coverage for a direct OpenAI or Google API call.

The user's reported AWS BAA must be confirmed in the intended account, along with service eligibility and configuration. Bedrock retention is model- and account-specific. Do not enable provider data sharing or change account-wide retention merely to make a model available. HIPAA eligibility is not certification of this application. No medical records should enter the new stack until these account-specific settings are checked.

Keep Google Vision in the existing Render app. Benchmark Textract and Vision on the same representative typed, handwritten, faint, rotated, and multi-column pages. Score exact service dates, laterality, levels, medications, negation, and page completeness against human transcriptions. An AWS-only data flow must use an AWS OCR route rather than quietly calling Google.

## Reliability changes prepared on this branch

Exact-text deduplication preserves distinct same-day encounters. Verification rejects missing inputs, reports unparsed/date-range entries as unreviewed, reviews complete matched source chunks in bounded groups, preserves mixed clean/error responses, and never claims that all clinical facts are verified. Session storage supports SESSION_DATA_DIR and can require a real mounted volume before startup.

These changes do not implement independent source-encounter reconciliation, full claim-level evidence validation, OCR completeness, or the merged UI. Source matching remains a date-based heuristic and human review is required.

## Storage and migration sequence

The inspected recursive Render service currently has no persistent disk. The repository's render.yaml disk declaration does not prove an existing manually created service has one. Existing ephemeral session data cannot be recovered by adding a new disk. Retrieve original records and any saved outputs from Dropbox; preserve the original downloaded Word/Markdown files.

For a Render pilot only: first attach a disk mounted at /app/data, then set SESSION_DATA_DIR=/app/data and REQUIRE_PERSISTENT_STORAGE=true. Enabling the guard without attaching the disk intentionally stops startup. Attaching the disk is a billable infrastructure change and is not performed by this branch. Keeping Render unchanged during the AWS build means this risk remains there.

Build the AWS environment under a separate name/domain, database, bucket, queue, and IAM roles. Use synthetic records first. Restore approved case copies from Dropbox into AWS without overwriting originals. Run the same manually checked cases through both paths. Exercise job interruption, retries, source downloads after login expiry, approval invalidation, and restore from backup. Only retire Render after user acceptance and verified export/restore. No deletion is part of the initial migration.

## Acceptance gates

Synthetic fixture: known encounters, two different events on the same date, billing-only dates, signature-date distractions, an unreadable page, conflicting notes, and a deliberately unsupported treatment sentence. All expected encounters must be accounted for; unsupported and unreviewed items must remain visible and prevent a reviewed designation. Tests must assert exact outcomes, not merely that the model request succeeded.

Run a representative case with human-adjudicated source dates and statements. Report missing encounters, unsupported statements, incorrect date roles, wrong provider attribution, OCR failures, and human review burden separately. Do not set a blanket accuracy percentage based on the model's own confidence.

Before production: confirm account and region, BAA and selected-model terms, team access rules, estimated monthly infrastructure and per-case processing cost, backup restoration, and the HTTPS domain. The current environment has no authenticated AWS account connection, so no cloud resources have been provisioned.

## Primary references checked

https://aws.amazon.com/compliance/hipaa-eligible-services-reference/
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-fable-5-1.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-openai.html
https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html
https://docs.aws.amazon.com/bedrock/latest/userguide/models-endpoint-availability.html
