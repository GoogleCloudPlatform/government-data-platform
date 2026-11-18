# AGENTS.md — Government Data Platform (GDP)

Multi-project GCP BigQuery data platform (Terraform + Composer 3 + BigQuery Remote Function + Cloud Tasks + Python 3.10 CF v2).

**API ingestion flow:** `GCS CSV` → Composer DAG (`api_workflow_<name>.py`) → BQ `routine_execute_api_fnc` → `api-connector` CF v2 (`BIGQUERY_ROUTINE` → Cloud Tasks → `CLOUD_TASK`) → external API → BQ `tbl_result_*` + `tbl_process_log_*` (+ optional Pub/Sub → `workflow-postprocess/<name>` CF v2).

## Structure

| Path | Role |
| --- | --- |
| `1-foundations/` | Root Terraform (`01-*.tf`–`08-*.tf`, `main.tf`, `locals.tf`, `apply.sh`): 10 projects (`drp`, `lod`, `orc`, `trf`, `cmn`, `exp`, `dwh-{lnd,cur,conf,plg}`), Composer 3, BQ routine, CF v2s. |
| `config/<workspace>/` | Active TF inputs (`config.yml`, `workflows.json`, `backend.conf`). Populated: `default`, `dev`; stubs: `qa`, `prd`. Template: `config/sample-configs/example.yml`. |
| `config/api-connector/` | `sample.csv`, `samples/client_credentials.json`, and non-workspace `config.json` uploaded to GCS (`02-api-connector.tf`). |
| `airflow/dags_template/` | `generic-api-service.py`, rendered via TF `templatefile()` to Composer GCS as `dags/api_workflow_<name>.py`. (`airflow/dags/` is unused.) |
| `api-connector/` | Python 3.10 CF v2 (`load-0-api-fnc`) for `BIGQUERY_ROUTINE` enqueueing and `CLOUD_TASK` HTTP execution. |
| `workflow-postprocess/<name>/` | Python 3.10 CF v2 per postprocessing workflow (template: `workflow1/`). |
| `tests/` | Stdlib `unittest` security & Terraform HCL regression tests (`test_*_security.py`). |
| `docs/` | Architecture, concepts, prerequisites, workflow docs (partially stale — see Gotchas). |

## Conventions & Patterns

- **Terraform:** Pin Fabric modules to `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/<name>?ref=v36.0.1` and `google`/`google-beta` providers to `6.12.0` (`user_project_override = true`). `google_composer_environment` uses `provider = google-beta`. Inputs come from `1-foundations/locals.tf` (`config/${terraform.workspace}/`); there is no `variables.tf` or `outputs.tf` (lone output `load-0-api-load-runner-sa` lives in `02-api-connector.tf`).
- **Naming:** Projects `<prefix>-<layer>[-<suffix>]`; BQ datasets `${replace(resource-prefix, "-", "_")}_<stage>_bq_0` (`dwh-load-bq-0` lives in `load-project`, declared in `05-datawarehouse.tf`); postprocessing topics `<prefix>-<workflow>-trf-ps-0` and CFs `workflow-postprocess-<workflow>`; Cloud Tasks queues `projects/<PROJECT_ID>/locations/<REGION>/queues/cloud-task-api-<id>`.
- **IAM & CF security (enforced by `tests/`):** Least-privilege per stage via SA impersonation (`roles/iam.serviceAccountTokenCreator`) and Google Groups (`data-engineers`, `data-analysts`, `data-security`); no static SA keys. Never grant `allUsers`/`allAuthenticatedUsers` in `1-foundations/*.tf`, never use `google_project_iam_binding` in `02-api-connector.tf`, and never grant project-wide `serviceAccountUser`, `run.invoker`, `cloudfunctions.invoker`, or `secretmanager.secretAccessor` to CF SAs (bind secrets per-secret via `google_secret_manager_secret_iam_member`). Every `cloud-function-v2` must set `ingress_settings = "ALLOW_INTERNAL_ONLY"`, SA-only function-scoped `roles/run.invoker`, and separate runtime (`*-runner-sa`) vs. invoker (`*-invoker-sa`) SAs. Cloud Tasks must attach an `OidcToken` (`TASK_INVOKER_SA`), and `load-0-api-fnc` enforces server-side `ALLOWED_WORKFLOWS` (`src/workflow_policy.py`) before reading secrets or calling outbound HTTPS URLs (`allow_redirects=False`).
- **Cloud Functions (`api-connector/`, `workflow-postprocess/*/`):** `python310`, `entry_point = "main"`, `memory = 128`, `timeout = 480`, pinned `requirements.txt`. Pattern: `main.py` (`@functions_framework.http`) → static `Handler.execute(request) -> Tuple[Any, int]` in `src/`, `src/config.py`, `src/logger.py` (`coloredlogs`), Python 3.10 `match`/`case`, relative imports (`from .src...`).
- **Headers & CLI:** Begin every `.tf`, `.py`, and `.sh` file with the Apache 2.0 `Copyright 2022 Google LLC` header. Use `gcloud storage` (never `gsutil`).

## Test & Validation Commands

No CI or linter is configured. Run before finishing changes:

```bash
# Security & Terraform HCL tests (repo root; stdlib-only, no venv; -B avoids untracked __pycache__)
python3 -B -m unittest discover -s tests -v

# Terraform formatting (repo root) & validation (inside 1-foundations/; init fetches Fabric from GitHub)
terraform fmt -check -recursive 1-foundations/
terraform init -backend=false && terraform validate

# Deploy / destroy workspace (inside 1-foundations/)
terraform workspace select <ws> || terraform workspace new <ws>
bash apply.sh            # or: bash apply.sh --destroy

# Local CF v2 smoke test (inside api-connector/ or workflow-postprocess/<name>/)
python3.10 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
PROJECT_ID=<proj> PROJECT_NUMBER=<num> REGION=<region> FUNCTION_NAME=load-0-api-fnc \
  TASK_INVOKER_SA=api-src-fnc-sa-invoker@<proj>.iam.gserviceaccount.com \
  ALLOWED_WORKFLOWS='{"workflow1":{"uri":"https://pokeapi.co/api/v2/pokemon/ditto","method":"GET","auth_type":"NONE"}}' \
  functions-framework --target main --debug
```

Local `BIGQUERY_ROUTINE` POST payloads require **8 positional strings** per `calls` entry starting with `workflow_id` (see `_make_call_row()` in `tests/test_api_connector_security.py`).

## What Newcomers Get Wrong

### Stale Docs & Config Traps
- **No `variables.tf` / `.tfvars`:** `1-foundations/README.md` and `docs/gdp-prerequisites.md` retain upstream Fabric docs; real config is `config/<workspace>/{config.yml,workflows.json,backend.conf}`. (`docs/gdp-architeture.md` is also misspelled, breaking repo links.)
- **Two workflow JSON files:** `config/<workspace>/workflows.json` drives DAGs, `ALLOWED_WORKFLOWS`, and postprocessing CFs; `config/api-connector/config.json` is uploaded separately to GCS (not workspace-scoped).
- **Auth docs contradict code:** `docs/gdp-workflow-config.md` documents `PUBLIC` and storing `auth_server`/`client_id` in Secret Manager. Actual code (`cloud_task.py`, `auth_type.py`) only has `CLIENT_CREDENTIALS`, `HTTP_BASIC`, and `NONE`: `CLIENT_CREDENTIALS` reads `auth_server`/`client_id` from request `auth` JSON (validated against `ALLOWED_WORKFLOWS`), expects only `{"client_secret": "..."}` in Secret Manager, and sends `Authorization: access_token <token>` (not `Bearer`). Omitting `auth` still crashes `generic-api-service.py` (whereas `cloud_task.py` accepts `NONE` without `secret_name`).
- **Stale `api-connector/README.md` `curl`:** Omits `workflow_id` (arg 1 of 8).
- **No root `.gitignore`:** Only `api-connector/.gitignore` exists — `unittest` without `-B`, `terraform init`, or `venv/` elsewhere leaves untracked files (`.terraform/`, `.terraform.lock.hcl`, `__pycache__/`).

### Cross-File Contracts
- **8-argument BQ routine order:** Keep exact argument order in sync across `02-api-connector.tf` (`routine_execute_api_fnc`), `airflow/dags_template/generic-api-service.py`, and `api-connector/src/handlers/bigquery.py` (`expected_args`).
- **`tbl_process_log` ↔ `tbl_result` swap:** The DAG passes `tbl_process_log_<ts>` as `result_table`; `bigquery.py` and `cloud_task.py` swap prefixes via `.replace()`. Never rename either prefix independently.
- **Postprocessing discovery & topic mapping:** `locals.tf` creates a Pub/Sub topic + CF only when a `workflows.json` `name` matches a non-empty `workflow-postprocess/<name>/` dir; `02-api-connector.tf` maps topics to `workflow_id` in `PUBSUB_TOPICS` by stripping `${resource-prefix}-` and `-trf-ps-0`.
- **DAG template (`generic-api-service.py`):** Escape literal `${...}` as `$${...}` for `templatefile()`. Never leave both `static_data` and `dynamic_data` empty for `headers`, `query_string`, or `body` — `_json_pairs()` generates invalid SQL (`CONCAT('{',,'}')`).
- **Tests parse Terraform HCL:** `tests/test_*_security.py` inspect `1-foundations/*.tf` directly — re-run `unittest` after any Terraform edit.

### Existing Quirks & Bugs
- **Hardcoded `"US"` BQ location:** `google_bigquery_connection` (`02-api-connector.tf`) and `BigQueryCreateEmptyTableOperator` (`generic-api-service.py`) hardcode `"US"`, ignoring `local.config.location` / `BQ_LOCATION`.
- **`apply.sh`:** `#!/bin/bash` is below the license header (run with `bash apply.sh`), the required-file loop checks undefined `${file_paths[@]}` instead of `${REQUIRED_CONFIG_FILES[@]}`, and `plan` uses `-lock=false`.
- **Multi-workflow bucket collision:** `transf-0-api-fnc` (`06-transformation.tf`) shares `bucket_name = "wfpp-src-${random_id.default.hex}"` across `for_each = toset(local.active-workflows)` — a second active workflow fails without unique/external buckets.
- **Terraform IAM & deletion gaps:** `drop-project.iam` (`01-dropoff.tf`) duplicates `"roles/storage.objectAdmin"` (second entry overwrites `load-sa-df-0`); `dwh-{cur,conf,plg}` omit `deletion_policy = "ABANDON"`.
- **CF runtime traps:** `src/config.py` requires `PROJECT_ID` at import time; `bigquery.py` and `cloud_task.py` fail closed unless `ALLOWED_WORKFLOWS` (plus `REGION`, `FUNCTION_NAME`, `TASK_INVOKER_SA` for enqueueing) is set and `queue_name` matches `ALLOWED_QUEUE_PREFIX` (`cloud-task-api-`). Run via `functions-framework --target main` (not `python main.py`). `Handler.execute()` propagates `400`/`403`/`422`/`503` rejections for `CLOUD_TASK` and returns `202` for executed tasks (suppressing Cloud Tasks retries on upstream errors), `cloud_task.py` passes `body` via `requests` `data=` (not `json=`), and `workflow1/main.py` uses `@functions_framework.http` (not `.cloud_event`).
