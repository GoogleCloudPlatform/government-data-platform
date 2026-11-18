# AGENTS.md — Government Data Platform (GDP)

Multi-project GCP BigQuery data platform (Terraform + Composer 3 + BigQuery Remote Function + Cloud Tasks + Python 3.10 Cloud Functions v2).

**API ingestion flow:** `GCS CSV` → Composer DAG (`generic-api-service.py`) → BQ `routine_execute_api_fnc` → `api-connector` CF v2 (`BIGQUERY_ROUTINE` → Cloud Tasks → `CLOUD_TASK`) → external API → BQ `tbl_result_*` + `tbl_process_log_*` (+ optional Pub/Sub → `workflow-postprocess/<name>` CF v2).

## Structure

| Path | Role |
| --- | --- |
| `1-foundations/` | Root Terraform (`01-*.tf`–`08-*.tf`): 10 GCP projects (`drp`, `lod`, `orc`, `trf`, `cmn`, `exp`, `dwh-{lnd,cur,conf,plg}`), Composer 3, BQ remote routine, CF v2s. |
| `config/<workspace>/` | Active TF inputs (`config.yml`, `workflows.json`, `backend.conf`). `default` and `dev` are populated; `qa`/`prd` are `.gitkeep` stubs. |
| `config/api-connector/` | `sample.csv`, `samples/client_credentials.json`, and non-workspace `config.json` uploaded to GCS (`02-api-connector.tf`). |
| `airflow/dags_template/` | `generic-api-service.py` — rendered via TF `templatefile()` to Composer GCS as `dags/api_workflow_<name>.py`. (`airflow/dags/` is unused.) |
| `api-connector/` | Python 3.10 CF v2 (`load-0-api-fnc`) handling `BIGQUERY_ROUTINE` enqueueing and `CLOUD_TASK` HTTP execution. |
| `workflow-postprocess/<name>/` | Python 3.10 CF v2 per postprocessing workflow (template: `workflow1/`). |
| `tests/` | Stdlib `unittest` security & Terraform regression tests (`test_api_connector_security.py`, `test_cloud_functions_auth_security.py`). |
| `docs/` | Architecture, concepts, prerequisites, workflow docs (partially stale — see Gotchas). |

## Conventions & Patterns

- **Terraform:** Pin Fabric modules to `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/<name>?ref=v36.0.1` and `google`/`google-beta` providers to `6.12.0` (`user_project_override = true` in `1-foundations/main.tf`). `google_composer_environment` uses `provider = google-beta`.
- **Workspace-driven inputs:** `1-foundations/locals.tf` loads `../config/${terraform.workspace}/{config.yml,workflows.json}`. There is no `variables.tf` or `outputs.tf` (the lone output `load-0-api-load-runner-sa` lives in `02-api-connector.tf`).
- **Naming:** Projects `<prefix>-<layer>[-<suffix>]`; BQ datasets `${replace(resource-prefix, "-", "_")}_<stage>_bq_0` (note `dwh-load-bq-0` lives in `load-project`, declared in `05-datawarehouse.tf`); Pub/Sub topics `<prefix>-<workflow>-trf-ps-0`; Cloud Tasks queues `projects/<PROJECT_ID>/locations/<REGION>/queues/cloud-task-api-<id>`.
- **IAM & security:** Least-privilege per stage via SA impersonation (`roles/iam.serviceAccountTokenCreator`) and Google Groups (`data-engineers`, `data-analysts`, `data-security`); no static SA keys. Keep all Cloud Functions v2 (`load-0-api-fnc`, `transf-0-api-fnc`) locked down (`ALLOW_INTERNAL_ONLY`, function-scoped `roles/run.invoker`, separate runtime vs. invoker SAs, Cloud Tasks OIDC token).
- **Cloud Functions (`api-connector/`, `workflow-postprocess/*/`):** `python310`, `entry_point = "main"`, `memory = 128`, `timeout = 480`. Layout: `main.py` (`@functions_framework.http`) → static `Handler.execute(request) -> Tuple[Any, int]` in `src/`, `src/config.py`, `src/logger.py` (`coloredlogs`), pinned `requirements.txt`, Python 3.10 `match`/`case`, package-relative imports (`from .src...`).
- **Headers & CLI:** Begin every `.tf`, `.py`, and `.sh` file with the Apache 2.0 `Copyright 2022 Google LLC` header. Use `gcloud storage` (never `gsutil`).

## Test & Validation Commands

No CI or linter is configured. Run these checks before finishing changes:

```bash
# Unit & Terraform security regression tests (from repo root; stdlib only, no venv needed; -B avoids untracked tests/__pycache__/)
python3 -B -m unittest discover -s tests -v

# Terraform formatting & validation (inside 1-foundations/; init fetches GitHub Fabric modules)
terraform fmt -check -recursive
terraform init -backend=false && terraform validate

# Deploy / destroy a workspace (inside 1-foundations/)
terraform workspace select <workspace> || terraform workspace new <workspace>
bash apply.sh            # or: bash apply.sh --destroy

# Local CF v2 smoke test (inside api-connector/ or workflow-postprocess/<name>/)
python3.10 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
PROJECT_ID=<proj> PROJECT_NUMBER=<num> REGION=<region> FUNCTION_NAME=load-0-api-fnc \
  TASK_INVOKER_SA=api-src-fnc-sa-invoker@<proj>.iam.gserviceaccount.com \
  functions-framework --target main --debug
```

For `BIGQUERY_ROUTINE` local POST payloads, pass **8 positional strings** per `calls` entry starting with `workflow_id` (see `_make_call_row()` in `tests/test_api_connector_security.py`).

## What Newcomers Get Wrong

### Stale Docs & Config Traps
- **No `variables.tf` / `.tfvars`:** `1-foundations/README.md` and `docs/gdp-prerequisites.md` carry upstream Fabric docs referencing `variables.tf`, `outputs.tf`, and `.tfvars`. Actual config lives in `config/<workspace>/{config.yml,workflows.json,backend.conf}`.
- **Two workflow JSON files:** Composer DAGs and postprocessing CFs come from `config/<workspace>/workflows.json`; `config/api-connector/config.json` is uploaded separately to GCS (`02-api-connector.tf`) and is not workspace-scoped.
- **Auth docs contradict code:** `docs/gdp-workflow-config.md` documents `PUBLIC` and storing `auth_server`/`client_id` in Secret Manager. Actual behavior (`api-connector/src/handlers/cloud_task.py`, `auth_type.py`): only `CLIENT_CREDENTIALS`, `HTTP_BASIC`, and `NONE` exist; `CLIENT_CREDENTIALS` reads `auth_server`/`client_id` from request `auth` JSON, Secret Manager holds only `{"client_secret": "..."}`, and requests send `Authorization: access_token <token>` (not `Bearer`). Omitting `auth` crashes both `generic-api-service.py:98` and `cloud_task.py:168`, and `NONE` still requires `secret_name`.
- **`api-connector/README.md` `curl` example is stale:** It omits `workflow_id` (arg 1 of 8).

### Fragile Cross-File Contracts
- **8-argument BQ routine order:** Keep exact argument order in sync across `02-api-connector.tf` (`routine_execute_api_fnc`), `airflow/dags_template/generic-api-service.py`, and `api-connector/src/handlers/bigquery.py` (`expected_args`).
- **`tbl_process_log` ↔ `tbl_result` swap:** The DAG passes `tbl_process_log_<ts>` as `result_table` (`generic-api-service.py:145`), and `bigquery.py` / `cloud_task.py` derive each table from the other via `.replace()`. Renaming either prefix breaks writes.
- **Postprocessing discovery & topic mapping:** `locals.tf:24-31` creates a Pub/Sub topic + CF only when a `workflows.json` `name` matches a non-empty `workflow-postprocess/<name>/` dir. `02-api-connector.tf:198-200` recovers `workflow_id` by stripping `${resource-prefix}-` and `-trf-ps-0` from topic names.
- **DAG template (`generic-api-service.py`):** Escape literal `${...}` as `$${...}` so TF `templatefile()` doesn't fail. Never leave both `static_data` and `dynamic_data` empty for `headers`, `query_string`, or `body` — `_json_pairs()` generates invalid SQL (`CONCAT('{',,'}')`).
- **Tests assert on Terraform source text:** `tests/test_api_connector_security.py` and `tests/test_cloud_functions_auth_security.py` inspect `1-foundations/*.tf` directly; always re-run `unittest` after Terraform edits.

### Existing Quirks & Bugs
- **Hardcoded `"US"` BQ location:** `02-api-connector.tf:19` (`google_bigquery_connection`) and `generic-api-service.py:219,247` ignore `local.config.location` / `BQ_LOCATION`.
- **`apply.sh`:** `#!/bin/bash` is on line 15 (after the license header — invoke via `bash apply.sh`), the required-file loop checks undefined `${file_paths[@]}` instead of `${REQUIRED_CONFIG_FILES[@]}`, and `plan` runs with `-lock=false`.
- **Multi-workflow bucket collision:** `06-transformation.tf:230` shares `bucket_name = "wfpp-src-${random_id.default.hex}"` across `for_each = toset(local.active-workflows)` — adding a second active workflow fails without a unique or external bucket.
- **Terraform IAM & deletion gaps:** `01-dropoff.tf:45,47` duplicates `"roles/storage.objectAdmin"` (second entry overwrites `load-sa-df-0`); `dwh-{cur,conf,plg}` omit `deletion_policy = "ABANDON"`.
- **CF runtime traps:** `src/config.py` requires `PROJECT_ID` at import time; `bigquery.py` fails closed unless `REGION`, `FUNCTION_NAME`, `TASK_INVOKER_SA` are set and `queue_name` matches `ALLOWED_QUEUE_PREFIX` (`cloud-task-api-`); run via `functions-framework --target main` (not `python main.py`); `handler.py:40-41` always returns HTTP `202` for `CLOUD_TASK` (suppressing retries even on auth/secret errors); `workflow1/main.py` uses `@functions_framework.http` (not `.cloud_event`).
