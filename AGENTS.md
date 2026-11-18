# AGENTS.md — Government Data Platform (GDP)

GCP reference architecture and Terraform blueprint for a multi-project BigQuery data platform with an Airflow + BigQuery Remote Function + Cloud Tasks REST API ingestion pipeline and Pub/Sub-triggered postprocessing.

**End-to-end API ingestion flow:**
`GCS CSV` → Composer DAG (`generic-api-service.py`) → BigQuery `routine_execute_api_fnc` → `api-connector` CF v2 (`BIGQUERY_ROUTINE` mode) → Cloud Tasks queue → `api-connector` CF v2 (`CLOUD_TASK` mode) → external API → BigQuery `tbl_result_*` + `tbl_process_log_*` (+ optional Pub/Sub → `workflow-postprocess/<name>` CF v2).

## Repository Structure

| Path | Purpose |
| --- | --- |
| `1-foundations/` | Root Terraform module deploying 10 GCP projects (`drp`, `lod`, `orc`, `trf`, `cmn`, `exp`, `dwh-{lnd,cur,conf,plg}`), Composer 3, BigQuery remote routine, Cloud Functions v2, and templated DAGs. Numbered `01-*.tf`–`08-*.tf` by stage. |
| `config/<workspace>/` | Per-workspace Terraform inputs (`config.yml`, `workflows.json`, `backend.conf`). `default` and `dev` are populated; `qa` and `prd` are `.gitkeep` stubs. |
| `config/api-connector/` | Sample CSV (`sample.csv`), `samples/client_credentials.json`, and `config.json` (uploaded to GCS by `02-api-connector.tf`, **not** what generates DAGs). |
| `airflow/dags_template/` | `generic-api-service.py` — Terraform `templatefile()` source rendered to Composer's GCS bucket as `dags/api_workflow_<name>.py`. (`airflow/dags/` is empty/unused by TF.) |
| `api-connector/` | Python 3.10 Cloud Function v2 (`load-0-api-fnc`) handling both `BIGQUERY_ROUTINE` enqueueing and `CLOUD_TASK` execution. |
| `workflow-postprocess/<name>/` | Python 3.10 Cloud Function v2 per postprocessing workflow (template: `workflow1/`). |
| `tests/` | Stdlib `unittest` security & regression tests covering `api-connector/` and `1-foundations/02-api-connector.tf`. |
| `docs/` | Architecture, concepts, prerequisites, and workflow config docs (note drift in Gotchas below). |

## Conventions & Patterns

- **Terraform modules & providers:** All Fabric modules are pinned to `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/<name>?ref=v36.0.1`. Providers `google` and `google-beta` are pinned to `6.12.0` with `user_project_override = true` (`1-foundations/main.tf`). `google_composer_environment` uses `provider = google-beta`.
- **Workspace-driven config:** `1-foundations/locals.tf` loads `../config/${terraform.workspace}/config.yml` via `yamldecode()` and `workflows.json` via `jsondecode()`. There is **no** `variables.tf` or `outputs.tf` (the single output `load-0-api-load-runner-sa` lives in `02-api-connector.tf`).
- **Naming conventions:**
  - Projects: `<prefix>-<layer>[-<suffix>]` (`drp`, `lod`, `orc`, `trf`, `cmn`, `exp`, `dwh-lnd`, `dwh-cur`, `dwh-conf`, `dwh-plg`).
  - BigQuery datasets: replace hyphens with underscores (`${replace(resource-prefix, "-", "_")}_<stage>_bq_0`). Note `dwh-load-bq-0` lives in `load-project` even though it is declared in `05-datawarehouse.tf`.
  - Postprocessing Pub/Sub topics: `<prefix>-<workflow_name>-trf-ps-0`.
  - Cloud Tasks queues: `projects/<PROJECT_ID>/locations/<REGION>/queues/cloud-task-api-<id>` (enforced by `ALLOWED_QUEUE_PREFIX` in `api-connector/src/handlers/bigquery.py`).
  - Service accounts & IAM: least-privilege per stage using SA impersonation (`roles/iam.serviceAccountTokenCreator`) and Google Groups (`data-engineers`, `data-analysts`, `data-security`); no static SA keys.
- **Python Cloud Functions (`api-connector/`, `workflow-postprocess/*/`):**
  - Runtime `python310`, `entry_point = "main"`, `memory = 128` MB, `timeout = 480`s.
  - Layout: `main.py` (`@functions_framework.http`) delegating to static `Handler.execute(request) -> Tuple[Any, int]` under `src/`, with `src/config.py` (env vars), `src/logger.py` (`coloredlogs`), and pinned `requirements.txt`.
  - Uses Python 3.10 `match`/`case` and package-relative imports (`from .src...`).
- **Headers & CLI:** Every `.tf`, `.py`, and `.sh` file begins with the Apache 2.0 `Copyright 2022 Google LLC` header. Use `gcloud storage` (not `gsutil`) in scripts/docs.

## Validation & Test Commands

There is no CI workflow or linter configured. Run the stdlib regression tests and Terraform checks below:

```bash
# Unit & Terraform security regression tests (run from repo root)
python3 -m unittest discover -s tests -v

# Terraform formatting & static validation (run inside 1-foundations/)
terraform fmt -check -recursive
terraform init -backend=false
terraform validate

# Deploy via workspace (select workspace first; invoke via bash, see Gotchas)
terraform workspace select <workspace> || terraform workspace new <workspace>
bash apply.sh            # init with ../config/<ws>/backend.conf + plan + interactive apply
bash apply.sh --destroy  # destroy active workspace

# Local Cloud Function smoke test (run inside api-connector/ or workflow-postprocess/<name>/)
python3.10 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
PROJECT_ID=<project> PROJECT_NUMBER=<num> REGION=<region> FUNCTION_NAME=load-0-api-fnc \
  TASK_INVOKER_SA=api-src-fnc-sa-invoker@<project>.iam.gserviceaccount.com \
  functions-framework --target main --debug
```

Local `curl` test for `api-connector` `BIGQUERY_ROUTINE` mode requires **8 positional args** per call (`workflow_id` first — `api-connector/README.md` omits it) and a queue matching `projects/<PROJECT>/locations/<REGION>/queues/cloud-task-api-<id>`:

```bash
curl -X POST http://localhost:8080 -H "Content-Type: application/json" -d '{
  "calls": [[
    "workflow1",
    "{\"uri\":\"https://pokeapi.co/api/v2/pokemon/ditto\",\"method\":\"GET\"}",
    "{\"type\":\"HTTP_BASIC\",\"secret_name\":\"projects/<NUM>/secrets/<NAME>/versions/latest\"}",
    "{\"Content-Type\":\"application/json\"}",
    "{\"q1\":\"val\"}",
    "{\"key1\":\"val\"}",
    "<dataset>.tbl_result_20250101000000",
    "projects/<PROJECT>/locations/<REGION>/queues/cloud-task-api-20250101000000"
  ]]
}'
```

## Newcomer Gotchas

### Stale Docs & Config Traps
- **No `variables.tf` / `terraform.tfvars`:** `1-foundations/README.md` and `docs/gdp-prerequisites.md` retain upstream Fabric docs referencing `variables.tf`, `outputs.tf`, and `.tfvars`. Actual configuration is `config/<workspace>/config.yml` + `workflows.json` + `backend.conf`.
- **Two workflow JSON files:** Composer DAGs and postprocessing CFs are generated from `config/<workspace>/workflows.json`. `config/api-connector/config.json` is uploaded separately to GCS (`02-api-connector.tf`) and is **not** workspace-scoped.
- **Postprocessing auto-discovery:** A workflow gets a Pub/Sub topic and postprocessing CF **only if** its `name` in `workflows.json` matches a directory `workflow-postprocess/<name>/` containing at least one file (`locals.tf:24-31`).
- **Auth docs contradict code:** `docs/gdp-workflow-config.md` documents `type: "PUBLIC"` and storing `auth_server`/`client_id` inside Secret Manager. In reality (`api-connector/src/handlers/cloud_task.py`, `auth_type.py`):
  - Only `CLIENT_CREDENTIALS`, `HTTP_BASIC`, and `NONE` exist in `AuthType`.
  - For `CLIENT_CREDENTIALS`, `auth_server` and `client_id` come from the request `auth` JSON; Secret Manager holds only `{"client_secret": "..."}`. The outbound header uses `Authorization: access_token <token>` (not `Bearer`).
  - Omitting `auth` crashes (`AuthType[""]` raises `KeyError`), and `type: "NONE"` still requires `secret_name`.

### Fragile Cross-File Contracts
- **8-argument BQ routine order:** `1-foundations/02-api-connector.tf` (`routine_execute_api_fnc`), `airflow/dags_template/generic-api-service.py`, and `api-connector/src/handlers/bigquery.py` (`expected_args`) must keep the exact same 8 arguments in order.
- **`tbl_process_log` ↔ `tbl_result` string swap:** The DAG intentionally passes `tbl_process_log_<ts>` as `result_table` (`generic-api-service.py:145`), and `bigquery.py` / `cloud_task.py:158-161` derive each table from the other via `.replace("tbl_process_log", "tbl_result")` and vice versa. Renaming either prefix breaks writes.
- **Pub/Sub topic name parsing:** `02-api-connector.tf` builds `PUBSUB_TOPICS` by stripping `${resource-prefix}-` and `-trf-ps-0` from topic names to map back to `workflow_id`. Keep this naming intact.
- **`generic-api-service.py` is a Terraform template:** `${WORKFLOW_CONFIG}` and `${WORKFLOW_KEY}` are interpolated by Terraform `templatefile()`. Escape any literal `${...}` as `$${...}` or `terraform plan` will fail. Also, `_json_pairs()` generates invalid SQL (`CONCAT('{',,'}')`) if both `static_data` and `dynamic_data` are empty for any of `headers`, `query_string`, or `body`.

### Known Quirks & Existing Bugs to Watch For
- **Hardcoded `"US"` BigQuery location:** `02-api-connector.tf:19` (`google_bigquery_connection`) and `generic-api-service.py:219,247` hardcode `"US"` instead of reading `local.config.location` / `BQ_LOCATION`.
- **`apply.sh` quirks:** `#!/bin/bash` is on line 15 (after the license header), the required-file loop iterates over undefined `${file_paths[@]}` instead of `${REQUIRED_CONFIG_FILES[@]}`, and `terraform plan` runs with `-lock=false`.
- **Multiple postprocessing workflows collide on bucket name:** `06-transformation.tf:213-228` uses `for_each = toset(local.active-workflows)` with a non-unique `bucket_name = "wfpp-src-${random_id.default.hex}"`. Adding a second active workflow requires a per-workflow bucket name or a shared external bucket.
- **Terraform IAM & deletion quirks:**
  - `01-dropoff.tf:45,47` defines `"roles/storage.objectAdmin"` twice in `drop-project.iam` (second entry overwrites `load-sa-df-0`).
  - `load-0-api-fnc-runner-sa` publishes to `transf-project` Pub/Sub topics in `cloud_task.py:209`, but Terraform never grants it `roles/pubsub.publisher`.
  - `dwh-cur`, `dwh-conf`, and `dwh-plg` omit `deletion_policy = "ABANDON"` (unlike the other 7 projects).
  - `load-0-api-fnc` is locked down (`ALLOW_INTERNAL_ONLY`, function-scoped `roles/run.invoker`, Cloud Tasks OIDC token), whereas `transf-0-api-fnc` in `06-transformation.tf` still sets `ingress_settings = "ALLOW_ALL"` and `"roles/run.invoker" = ["allUsers"]` — lock down before production.
- **Cloud Function runtime quirks:**
  - `src/config.py` checks `PROJECT_ID` at **import time** (fails immediately if unset); `bigquery.py` also requires `REGION`, `FUNCTION_NAME`, and `TASK_INVOKER_SA` (plus `PROJECT_NUMBER` for deterministic `.run.app` URLs) and enforces `ALLOWED_QUEUE_PREFIX` (`cloud-task-api-`) on `queue_name`.
  - Run via `functions-framework --target main` (running `python main.py` directly fails on `from .src...` relative imports).
  - `api-connector/src/handler.py:40-41` discards inner status codes from `CloudTaskRequest.execute()` and always returns HTTP `202` (to suppress Cloud Tasks retries), even on auth/secret failures.
  - `workflow-postprocess/workflow1/main.py` uses `@functions_framework.http` (receiving Eventarc Pub/Sub CloudEvents as HTTP POST), not `@functions_framework.cloud_event`.
