# AGENTS.md — Government Data Platform (GDP)

Multi-project GCP BigQuery platform: Terraform + Cloud Foundation Fabric (`v36.0.1`), Cloud Composer 3, BigQuery Remote Function, Cloud Tasks, and Python 3.10 Cloud Functions v2.

**API ingestion flow:** GCS CSV → Composer DAG (`api_workflow_<name>.py`) → BQ `routine_execute_api_fnc` → `load-0-api-fnc` (`BIGQUERY_ROUTINE` → OIDC Cloud Task → `CLOUD_TASK`) → external HTTPS API → BQ `tbl_result_<id>` & `tbl_process_log_<id>` (+ optional Pub/Sub → Eventarc → `workflow-postprocess-<name>`).

## Structure

| Path | Role |
| --- | --- |
| `1-foundations/` | Root Terraform (`01`–`08`, `main.tf`, `locals.tf`, `backends.tf`, `apply.sh`) for 10 projects (`drp`, `lod`, `orc`, `trf`, `cmn`, `exp`, `dwh-{lnd,cur,conf,plg}`), Composer 3, BQ routine, and CF v2s. |
| `config/<workspace>/` | Workspace TF inputs: `config.yml`, `workflows.json`, `backend.conf` (`default/`, `dev/` active; `qa/`, `prd/` stubs; template: `config/sample-configs/example.yml`). |
| `config/api-connector/` | Non-workspace `config.json` uploaded to GCS (`02-api-connector.tf`), `sample.csv`, and `samples/client_credentials.json`. |
| `airflow/dags_template/` | `generic-api-service.py`, rendered via `templatefile()` to Composer `dags/api_workflow_<name>.py` (`airflow/dags/` is unused). |
| `api-connector/` | `load-0-api-fnc` CF v2 (`main.py`, `src/handler.py`, `src/handlers/{bigquery,cloud_task}.py`, `src/workflow_policy.py`). |
| `workflow-postprocess/<name>/` | Postprocess CF v2 per active workflow (copy `workflow1/` as template). |
| `tests/` | Stdlib-only `unittest` security, AST, and Terraform HCL guards (6 files, 70 tests). |
| `docs/` | Architecture, concepts, prerequisites, workflow guides (partially stale — verify against code). |

## Commands

No CI or linter is configured. Run before finishing changes:

```bash
# Security & unit tests (repo root; Python 3.10+, stdlib-only; -B avoids untracked __pycache__)
python3 -B -m unittest discover -s tests -v

# Terraform format (repo root) & validate (in 1-foundations/; init creates untracked .terraform/)
terraform fmt -check -recursive 1-foundations/
terraform init -backend=false && terraform validate

# Deploy / destroy workspace (in 1-foundations/)
terraform workspace select <ws> || terraform workspace new <ws>
bash apply.sh            # or: bash apply.sh --destroy

# Local CF v2 (in api-connector/ or workflow-postprocess/<name>/)
python3.10 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
PROJECT_ID=<proj> PROJECT_NUMBER=<num> REGION=<region> FUNCTION_NAME=load-0-api-fnc \
  TASK_INVOKER_SA=api-src-fnc-sa-invoker@<proj>.iam.gserviceaccount.com \
  ALLOWED_BQ_DATASET=<prefix>_dwh_load_bq_0 \
  ALLOWED_WORKFLOWS='{"workflow1":{"uri":"https://pokeapi.co/api/v2/pokemon/ditto","method":"GET","auth_type":"NONE"}}' \
  functions-framework --target main --debug
```

## Conventions & Patterns

- **Terraform:** Pin Fabric modules to `?ref=v36.0.1` and `google`/`google-beta` to `6.12.0` (`user_project_override = true`); Composer 3 uses `provider = google-beta`. Config loads via `1-foundations/locals.tf` (no `variables.tf`/`outputs.tf`; sole output `load-0-api-load-runner-sa` is in `02-api-connector.tf`).
- **Naming:** Projects `<prefix>-<layer>[-<suffix>]`; BQ datasets `${replace(resource-prefix, "-", "_")}_<stage>_bq_0` (`dwh-load-bq-0` lives in `load-project`, declared in `05-datawarehouse.tf`); topics `<prefix>-<wf>-trf-ps-0`; postprocess CFs `workflow-postprocess-<wf>`; queues `projects/<PROJECT_ID>/locations/<REGION>/queues/cloud-task-api-<id>`.
- **Cloud Functions:** Python 3.10 (`match`/`case`), `entry_point = "main"`, pinned `requirements.txt`, `src/{config,logger,handler}.py`, relative imports (`from .src...`), `Handler.execute(request) -> Tuple[Any, int]`.
- **Tests:** Keep `tests/test_*_security.py` stdlib-only via `_install_optional_stubs()`. Mock `src.workflow_policy.socket.getaddrinfo` for `CloudTaskRequest` (live DNS). Load CF `main.py` via synthetic `importlib.util` packages (`test_error_reflection_security.py`).
- **Headers & CLI:** Prefix every `.tf`, `.py`, `.sh` with the Apache 2.0 `Copyright 2022 Google LLC` header. Use `gcloud storage`, never `gsutil`.

## Security Invariants (Enforced by `tests/`)

Tests parse `1-foundations/*.tf` directly and inspect CF Python ASTs:

- **Private CF ingress & IAM:** Never use `allUsers`, `allAuthenticatedUsers`, `ALLOW_ALL`, or `google_project_iam_binding` (`02-api-connector.tf`). Every `cloud-function-v2` must set `ingress_settings = "ALLOW_INTERNAL_ONLY"` and function-scoped `roles/run.invoker` (`serviceAccount:` only).
- **Least-privilege SAs:** Keep runtime (`*-runner-sa`) and invoker (`*-invoker-sa`) SAs separate; never reuse a trigger SA as runtime SA. Never grant project-wide `iam.serviceAccountUser`, `run.invoker`, `cloudfunctions.invoker`, `secretmanager.secretAccessor`, or `bigquery.{dataEditor,dataOwner,admin}` to CF SAs (scope `secretAccessor` per secret, `serviceAccountUser` per SA, `bigquery.dataEditor` to `dwh-load-bq-0`). Use SA impersonation (`serviceAccountTokenCreator`) + Google Groups (`data-engineers`, `data-analysts`, `data-security`); never static SA keys.
- **Fail-closed `api-connector`:** Enqueued tasks must attach `OidcToken` (`TASK_INVOKER_SA`). Both handlers must exact-match via `WorkflowPolicy.authorize()` (`ALLOWED_WORKFLOWS`), validate `queue_name`, `result_table` (`<ALLOWED_BQ_DATASET>.(tbl_result_|tbl_process_log_)<run_id>`, also enforced in `Utils.save_bigquery()`), and DNS-resolved public HTTPS URLs (`validate_resolved_https_url`), sanitize headers (strip metadata/hop-by-hop + `Authorization` when authenticated; reject CRLF), and set `allow_redirects=False`.
- **No error/body reflection (AST-checked):** Never return upstream bodies or caught exceptions. `test_error_reflection_security.py` automatically scans `api-connector` and all `workflow-postprocess/*` `main.py` + `src/` handlers: `flask.Response(...)` in `except` must be a static string, and `return`/`log_info`/`replies.append` in `except` cannot reference the exception variable.

## What Newcomers Get Wrong

### Config, Docs & Formatting
- **Stale docs:** `1-foundations/{README,IAM}.md` and `docs/gdp-prerequisites.md` cite nonexistent `variables.tf`/`outputs.tf`/`.tfvars`; `docs/gdp-concepts.md` cites nonexistent local `terraform-modules/`; `docs/gdp-architeture.md` is misspelled (links point to `gdp-architecture.md`); `api-connector/README.md` `curl` has 7 args instead of 8 (omits `workflow_id` — see `_make_call_row()` in `tests/test_api_connector_security.py`).
- **Two workflow JSONs:** `config/<workspace>/workflows.json` drives DAGs, `ALLOWED_WORKFLOWS`, and postprocess CFs (replace sample bucket/secret IDs; edits need `terraform apply` + per-secret `secretAccessor` or return `403`). `config/api-connector/config.json` is uploaded separately to GCS (non-workspace).
- **Auth docs ≠ code:** `docs/gdp-workflow-config.md` lists `PUBLIC` and puts `auth_server`/`client_id` in Secret Manager. Actual `AuthType` is `CLIENT_CREDENTIALS`, `HTTP_BASIC`, or `NONE`: `CLIENT_CREDENTIALS` reads `auth_server`/`client_id` from `workflows.json`, expects `{"client_secret"}` in Secret Manager, and sends `Authorization: access_token <token>` (not `Bearer`); `HTTP_BASIC` expects `{"username", "password"}`. Omitting `auth` crashes `generic-api-service.py` — pass `{"type": "NONE"}`.
- **No root `.gitignore` & `fmt`-coupled tests:** Only `api-connector/.gitignore` exists (`unittest` without `-B`, `terraform init`, or `venv/` elsewhere creates untracked files). Security tests also assert exact `terraform fmt`-aligned strings in `02-api-connector.tf` (`TASK_INVOKER_SA`, `ALLOWED_BQ_DATASET`) — always run `terraform fmt`.

### Cross-File Contracts
- **8-arg BQ routine order:** Keep `02-api-connector.tf`, `generic-api-service.py`, and `bigquery.py` in sync: `workflow_id, request_config, auth, headers, query_string, body, result_table, queue_name`.
- **`tbl_process_log` ↔ `tbl_result` pair:** The DAG passes `<LOD_BQ_DATASET>.tbl_process_log_<ts>` as `result_table`; `resolve_result_and_log_tables()` validates it against `ALLOWED_BQ_DATASET` and maps to canonical `(tbl_result_<id>, tbl_process_log_<id>)`.
- **Postprocess activation & topic parsing:** `locals.tf` activates a workflow only when `workflows.json` `name` matches a `workflow-postprocess/<name>/` dir with a direct child file. `02-api-connector.tf` strips `${resource-prefix}-` and `-trf-ps-0` from topic names for `PUBSUB_TOPICS` (avoid those substrings in workflow names).
- **DAG template (`generic-api-service.py`):** Escape literal `${...}` as `$${...}`. Never leave both `static_data` and `dynamic_data` empty for `headers`, `query_string`, or `body` (`_json_pairs()` emits invalid SQL `CONCAT('{',,'}')`). `max_dispatch`, `response_config`, and `process_response.dataform_config` are unwired; `gcs_to_bigquery_table_expiration` is commented out.
- **Unused legacy SAs:** `load-0-api-sa`, `data-workflow-trigger-api-fnc-runner-sa` (`02-api-connector.tf`), and `sa-udf-bq` (`03-load.tf`) are unattached; active CF SAs are `load-0-api-fnc-{runner,invoker}-sa` and `transf-0-api-fnc-{runner,invoker}-sa`.

### Existing Bugs & Runtime Quirks
- **Hardcoded `"US"` BQ location:** `google_bigquery_connection` (`02-api-connector.tf`) and both `BigQueryCreateEmptyTableOperator` tasks (`generic-api-service.py`) ignore `local.config.location` / `BQ_LOCATION`.
- **`apply.sh`:** `#!/bin/bash` is below the license header (run `bash apply.sh`), required-file check loops over undefined `${file_paths[@]}` instead of `${REQUIRED_CONFIG_FILES[@]}`, and `plan` uses `-lock=false`.
- **Postprocess collision & bare SA:** `transf-0-api-fnc` (`06-transformation.tf`) shares `bucket_name = "wfpp-src-${random_id.default.hex}"` across `for_each` (2nd active workflow collides); `transf-0-api-fnc-runner-sa` has no default IAM roles.
- **TF IAM & deletion gaps:** `drop-project.iam` (`01-dropoff.tf`) duplicates `"roles/storage.objectAdmin"` (overwriting `load-sa-df-0`); `dwh-{cur,conf,plg}-project` omit `deletion_policy = "ABANDON"`.
- **CF runtime traps:** `src/config.py` requires `PROJECT_ID` at import time (run via `functions-framework --target main`, not `python main.py`) and defaults `ENVIRONMENT="local"` (DEBUG logging in deployed CFs unless set). For `CLOUD_TASK`, `Handler.execute()` propagates `400`/`403`/`422`/`503` and maps other statuses (including `500`s) to `202` to stop retries. `cloud_task.py` passes `body` via `requests` `data=` (not `json=`). `workflow1/main.py` uses `@functions_framework.http` (not `.cloud_event`).
