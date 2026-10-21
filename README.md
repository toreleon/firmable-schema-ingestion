# Reviewable business ingestion

A small runtime agent that inspects unfamiliar resources, proposes ontology mappings,
executes them on withheld records, critiques/revises its proposal, and stops for review.
One generic engine executes every mapping. Mappings in `outputs/mappings/` are model
outputs, never hand-authored source transformations.

Current submission (partial): 638 catalogue records → 50 shortlisted datasets;
six agent-generated mappings; 77,860 observations; 110 proposed links and 107
profile-eligible links supporting 50 company profiles.

The current matcher has a 50-link correctness audit: **50/50 correct at threshold
0.98**, with **all 50 correctness verdicts human-verified by the user**. This is a development/convenience sample, not an
independent population precision estimate. See `outputs/audit_summary_submission.json`
and `outputs/review/link_audit_submission.csv` for labels, attribution and reasoning.
The earlier 47-item approval packet is retained separately as approval history.

The 20-dataset audit is **incomplete**: 12 positive, three negative and five
inconclusive. Conditional false-positive rate is 3/15 = 20%; the full 20-item rate
is unknown, bounded by 15–40%. Latest source retrieval evidence is retained in
`outputs/review/triage_recheck/`. The five missing dataset verdicts remain an explicit limit of this submission.

Actual Haiku spend is **$8.0687**, checked and confirmed by the user. The historical
59/60-request ledger remains unchanged. A separate non-resettable demo allowance
provides six requests with a $1 reservation limit; no new paid calls have been made.
The demo's full runtime path is integration-tested with a synthetic provider;
a live seventh-source run requires privately configured provider credentials.

[WRITEUP.md](WRITEUP.md) and [WRITEUP.pdf](WRITEUP.pdf) are the two-page report.
[SOURCE_APPENDIX.md](SOURCE_APPENDIX.md) contains all 50 catalogue URLs.

## Run from a clean checkout (under 10 minutes)

Python 3.11 or newer. No API key or network access is needed for the included replay
after installing dependencies. Public data snapshots are included as compressed files.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -c requirements.txt -e .
python -m ingest replay
python -m ingest audit --version submission
python -m ingest cost
```

The included six configs are approved by the user at their exact payload hashes.
Replay checks those hashes and runs without `--draft`. New or edited mappings
require approval, or explicit `--draft` mode for review. Replay reconstructs raw files from
the included snapshots, checks SHA-256 hashes, extracts observations, resolves
identities, assembles profiles, and measures source-removal effects. It makes no
model calls. The exact current counts and unfinished requirements are recorded in
`outputs/replay_summary.json`; zero-yield sources are not silently counted as successes.

## Regenerate discovery and inference

```bash
python -m ingest discover
python -m ingest acquire-all
export ANTHROPIC_API_KEY='set this privately in your environment'
python -m ingest onboard-all --provider anthropic --model claude-haiku-5-5
python -m ingest replay --draft
python -m ingest evidence
```

Discovery ranks at least 200 distinct retrieved catalogue records using deterministic
metadata signals. Its score is an uncalibrated ranking heuristic, not measured precision.
`selected_sources.json` chooses resource IDs and download representations only; it
contains no field mappings. Dataset selection is deliberately separate from ranking.
The national catalogue returned HTTP 403. Queensland and NSW state portals were used.
Original Queensland file downloads returned WAF challenges; official public DataStore
CSV exports and nested JSON responses were available. These representations and the
original resource URLs are recorded in the source manifests.

Onboarding normally uses two model calls (proposal, critique/revision) plus deterministic
inspection/profile/validation tools. Ambiguous document parsing, invalid output, or
zero yield can trigger a bounded additional repair call. There is no API call per record.
Receipts contain model IDs, input/output tokens, requests, responses, execution feedback,
and timings. They contain no credential headers. Configure `INGEST_ANTHROPIC_KEY_FILE`
instead of `ANTHROPIC_API_KEY` if your secret is stored outside the repository.
`--provider deepseek` and an optional local GGUF backend are also supported; the latter
requires installing `llama-cpp-python` separately. Live inference duration depends on
the provider; the under-ten-minute path above is cached-data replay, not a model benchmark.

## Onboard a seventh source

Download a public resource into this checkout. Create a metadata JSON containing
`source_id`, `local_path`, `resource_url`, `resource_sha256`, `observed_at`,
`observed_at_basis`, `downloaded_at`, `licence`, and `source_reliability`.
These are acquisition/provenance metadata, not mappings. Then run:

```bash
python -m ingest onboard --source path/to/source_metadata.json --limit 1000
```

For the seventh-source demo, explicitly select the separate allowance:

```bash
python -m ingest onboard --source path/to/source_metadata.json --limit 1000 \
  --provider anthropic --model claude-haiku-5-5 \
  --budget-scope demo --demo-request-limit 6 --demo-usd-limit 1
```

The original `outputs/cost/budget.json` is preserved. Demo reservations and request
counts use `outputs/cost/demo_budget.json`; rerunning does not reset the allowance.
The demo ceiling plus the historical baseline must stay under $10. Cache hits make
no requests. Credentials must be configured privately. No live seventh-source run
is claimed in this submission.

No source-specific Python or mapping config is supplied. Generic readers support
CSV, XLSX (sheet/header selection), nested JSON/GeoJSON, JSONL, simple XML, and one
bounded ZIP member. Unsupported shapes stop for adapter review. PDF table extraction
is intentionally outside this implementation.

## Review and approve

`outputs/runs/<source_id>/08_review_packet.json` shows mappings, rationales, samples,
holdout execution, errors, unmapped fields, source/licence metadata, and semantic risks.
`outputs/MAPPING_REVIEW.md` provides a compact index. Approve an exact mapping hash:

```bash
python -m ingest approve --source-id DATASET_ID --reviewer YOUR_NAME \
  --config-sha256 HASH_FROM_REVIEW_INDEX --notes 'Checked roles, samples and unmapped fields'
```

`reject` records a reason. Approval is a local review record, not an authenticated
multi-user approval service. Any mapping edit invalidates its approval hash. Never
approve a config merely because type or checksum validation passed.

The user confirmed personal review and approval of the original 50 links and 20
triage assessments. Final attribution is human; the earlier AI-prepared assessments
are retained under `previous_review` and in `outputs/experiments/before_user_confirmation/`.
The five inconclusive cases retain blank verdicts. The original review does not
approve later changes. The user subsequently reviewed and approved the current
v2 sample and all six exact-hash mapping configs; this is recorded separately in
`outputs/user_confirmations.json`. Review the fixed seeded
20-item sample in `outputs/triage_audit.jsonl` using `outputs/review/triage_evidence.jsonl`.
Fill `contains_business_entities`, `reviewer`, `review_notes`, and evidence URL.
For `outputs/link_audit.jsonl`, fill `correct`, `reviewer`, and `review_notes` using
`outputs/review/link_evidence.jsonl`. Run `python -m ingest audit` to calculate the
sample false-positive rate and link precision. Unreviewed labels remain null. The
system does not describe AI-generated checks or rule scores as human-measured precision.
Alternatively, fill the 70 rows in `outputs/review/audit_results.csv` with `true`/`false`,
reviewer and notes, then run `python -m ingest import-audit --csv outputs/review/audit_results.csv`.
Run `python -m ingest replay` to apply rejection quarantine and rebuild profiles. Blank inconclusive verdicts retain their notes and stay outside the conclusive denominator. Explicit `reviewer_kind=human` is recorded for the user-confirmed original review; the local review record is not an authentication service.
Replay preserves the frozen link sample and its labels. Discovery/reranking creates a new triage template, so archive completed triage work before reranking. Evidence export refuses to overwrite a CSV containing decisions.

## Current 50-link correctness audit

`outputs/link_audit_submission.jsonl` freezes 50 proposals of the submitted matcher.
All retained raw pairs from the original human audit were carried forward,
regardless of their old verdict; six additional proposals were selected with a
fixed seed. Labels were reused only for identical source-record pairs. The six additional checks were initially prepared by AI; the user subsequently
confirmed human verification of all 50 correctness verdicts. Raw names, identifiers,
subject/capacity evidence and record hashes remain documented. The six prior AI
assessments are retained in per-row `review_history`, separately from final attribution.

```bash
python -m ingest import-audit --version submission --csv outputs/review/link_audit_submission.csv
python -m ingest audit --version submission
python -m ingest replay
```

This sample has 50 explicit true/false verdicts; approval-only rows cannot contribute
to measured precision. Its final reviewer attribution is **50 human / zero AI**. The user confirmation
is recorded in `outputs/user_confirmations.json`; earlier assessment provenance is preserved.
This is a development sample and does not establish population precision.
The five unresolved triage verdicts remain blank and excluded from the conditional
denominator. Missing resources are not automatically false positives.

## Render the two-page report

Install pandoc and WeasyPrint's platform libraries, then:

```bash
pip install -r requirements-render.txt
python scripts/render_writeup.py
```

The script renders the Markdown to A4 PDF using 10.5 pt body type, checks that the
result has exactly two pages and records source/PDF hashes in
`outputs/writeup_render_validation.json`. The 50-row source appendix is separate.

## Important semantics and limitations

* `firmable_ontology.yaml` is the user-provided contract. Targets use exact dotted
  paths such as `entity.abn`; each output observation contains one canonical claim,
  the required envelope, separate confidence dimensions, and raw-field provenance.
* `observed_at` uses record publication time if mapped. Otherwise resource/catalogue
  modification time is an explicitly labelled, imperfect publication proxy.
  `ingested_at` is the actual snapshot download time. We never claim they mean the same thing.
* Missing/unconfident fields are omitted. Invalid identifiers go to field-error output.
  Compound identifiers can yield isolated identifier-only party observations; names
  and addresses are never joined to those parties by position.
* A uniform 0.8 publisher prior and model mapping scores are uncalibrated assumptions.
  Link scores are separate uncalibrated rule scores. None is a precision estimate.
* Resolution uses a default 0.98 rule-score threshold: checksum-valid exact ABN scores
  0.995 and checksum-valid exact ACN scores 0.985. Explicit, unambiguous source-stated
  ABN/ACN co-occurrence can bridge the two namespaces; an ABN suffix is never used.
  The raw subject names must match after punctuation and corporate-suffix normalization.
  Explicit type/capacity conflicts, compound subjects, missing name corroboration and
  conflicting ACNs cause abstention. Employee/person and trustee records cannot
  introduce identifier bridges. Independently supported incompatible subjects cannot
  be unioned into one profile. Name-only candidates always abstain.
* `outputs/identity_context/` preserves raw names/type evidence and snapshot/row hashes,
  including uncertain name mapping candidates. This context can veto a link without
  promoting a name/type into a canonical fact. The 0.8 field-confidence floor and all
  six agent-generated mappings remain unchanged. Package version is 0.2.0; the
  unchanged canonical extractor remains 0.1.0.
* Parent contracts are not parent companies. No unsupported ownership relationships
  are inferred; `outputs/relationships.jsonl` is intentionally empty.
* `outputs/proposed_links.jsonl` contains 110 versioned v2 proposals;
  `outputs/profile_links.jsonl` contains 107 eligible links. The three historical
  false reviews still quarantine six records across matcher versions, withholding
  three current incident/dependent edges. The original 94% result belongs to v1.
  Each profile identifies the matcher version and supporting link IDs. Code-bound release approval is rechecked at replay; the implementation fixes
  invalidate the earlier code-bound release approval while exact-hash mapping
  approvals remain valid. Historic rejection
  quarantine continues to take precedence over release approval.
* Profile conflicts retain alternative values and provenance. Source removal is a
  fixed-membership counterfactual with support-loss counts, not a complete graph rebuild.
* The first 70% of sampled records train the proposal; the final 30% initially remain
  unseen. After critique the holdout is debugging evidence, not an independent final test.
  A third immutable evaluation split is a planned improvement.
* Onboarding receipts and checkpoints resume model calls by request fingerprint.
  A changed raw/ontology identity requires archiving the old run rather than overwriting it.
  An unavailable model account fails closed; a completed source survives another source's failure.

See `WRITEUP.md`, `VALIDATION.md`, `DATA_SOURCES.md`, and the machine-readable summaries for measured
results, cost-accounting gaps, and remaining work. This is a time-boxed assignment
submission, not a claim of complete production readiness.

## Tests

```bash
pip install pytest
python -m pytest -q
```

Tests cover actual failure boundaries: checksum rejection, compound-party separation,
multiline CSV/nested JSON, traceable duplicate records, approval tampering, abstention,
profile conflicts, agent revision driven by withheld execution errors, raw subject/type/capacity guards, safe identifier bridges, incompatible profile unions and cross-version quarantine. Synthetic
test configs are fixtures, not any of the six submitted source mappings.
