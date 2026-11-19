# AGENTS.md — Government Data Platform (GDP)

Multi-project GCP BigQuery platform: Terraform + Cloud Foundation Fabric (`v36.0.1`), Composer 3, BigQuery Remote Function, Cloud Tasks, and Python 3.10 Cloud Functions v2.

**API ingestion flow:** `GCS CSV` → Composer DAG (`api_workflow_<name>.py`) → BQ `routine_execute_api_fnc` → `load-0-api-fnc` (`BIGQUERY_ROUTINE` → OIDC Cloud Task → `CLOUD_TASK`) → external HTTPS API → BQ `tbl_result_<ts>` + `tbl_process_log_<ts>` (+ optional Pub/Sub → Eventarc → `workflow-postprocess-<name>` CF v2).

## Structure

| Path | Role |
| --- | --- |
| `1-foundations/` | Root Terraform (`01-*.tf`–`08-*.tf`, `main.tf`, `locals.tf`, `backends.tf`, `apply.sh`) for 10 projects (`drp`, `lod`, `orc`, `trf`, `cmn`, `exp`, `dwh-{lnd,cur,conf,plg}`), Composer 3, BQ routine, and CF v2s. |
| `config/<workspace>/` | Active TF inputs (`config.yml`, `workflows.json`, `backend.conf`). Populated: `default/`, `dev/`; stubs: `qa/`, `prd/`. Template: `config/sample-configs/example.yml`. |
| `config/api-connector/` | `sample.csv`, `samples/client_credentials.json`, and non-workspace `config.json` uploaded to GCS (`02-api-connector.tf`). |
| `airflow/dags_template/` | `generic-api-service.py`, rendered via `templatefile()` to Composer GCS as `dags/api_workflow_<name>.py`. (`airflow/dags/` is unused.) |
| `api-connector/` | Python 3.10 CF v2 (`load-0-api-fnc`): `src/handlers/{bigquery,cloud_task}.py` + `src/workflow_policy.py`. |
| `workflow-postprocess/<name>/` | Python 3.10 CF v2 per postprocessing workflow (template: `workflow1/`). |
| `tests/` | Stdlib `unittest` security & Terraform HCL regression tests (`test_*_security.py`). |
| `docs/` | Architecture, concepts, prerequisites, workflow docs (partially stale — verify against code). |

## Commands

No CI or linter is configured. Run tests and formatting checks before finishing changes:

```bash
# Security & Terraform HCL tests (repo root; stdlib-only, no venv; -B avoids __pycache__)
python3 -B -m unittest discover -s tests -v

# Terraform formatting (repo root) & validation (inside 1-foundations/; init fetches Fabric)
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

## Conventions & Patterns

- **Terraform:** Pin Fabric modules to `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/<name>?ref=v36.0.1` and `google`/`google-beta` providers to `6.12.0` (`user_project_override = true`); `google_composer_environment` uses `provider = google-beta`. Inputs come from `1-foundations/locals.tf` (no `variables.tf`/`outputs.tf`; lone output `load-0-api-load-runner-sa` is in `02-api-connector.tf`).
- **Naming:** Projects `<prefix>-<layer>[-<suffix>]`; BQ datasets `${replace(resource-prefix, "-", "_")}_<stage>_bq_0` (`dwh-load-bq-0` lives in `load-project`, declared in `05-datawarehouse.tf`); postprocessing topics `<prefix>-<workflow>-trf-ps-0` and CFs `workflow-postprocess-<workflow>`; Cloud Tasks queues `projects/<PROJECT_ID>/locations/<REGION>/queues/cloud-task-api-<id>`.
- **Cloud Functions (`api-connector/`, `workflow-postprocess/*/`):** `python310`, `entry_point = "main"`, `memory = 128`, `timeout = 480`, pinned `requirements.txt`, `src/config.py`, `src/logger.py` (`coloredlogs`), Python 3.10 `match`/`case`, relative imports (`from .src...`). Pattern (`api-connector/main.py` → `api-connector/src/handler.py`):
  ```python
  @functions_framework.http
  def main(request: flask.Request) -> flask.typing.ResponseReturnValue:
      (msg, code) = Handler.execute(request)  # static method -> Tuple[Any, int]
      return flask.Response(msg + "\n"), code
  ```
- **Tests:** Keep `tests/test_*_security.py` stdlib-only by stubbing optional deps (`_install_optional_stubs()` in `tests/test_api_connector_security.py`).
- **Headers & CLI:** Begin every `.tf`, `.py`, and `.sh` file with the Apache 2.0 `Copyright 2022 Google LLC` header. Use `gcloud storage` (never `gsutil`).

## Boundaries

Actively enforced by `tests/test_*_security.py` (which parse `1-foundations/*.tf` directly):

- **No public access:** Never use `allUsers`, `allAuthenticatedUsers`, or `ALLOW_ALL` in `1-foundations/*.tf`. Every `cloud-function-v2` must set `ingress_settings = "ALLOW_INTERNAL_ONLY"` and function-scoped `roles/run.invoker` for `serviceAccount:` principals only.
- **Least-privilege IAM:** Use SA impersonation (`roles/iam.serviceAccountTokenCreator`) and Google Groups (`data-engineers`, `data-analysts`, `data-security`); never create static SA keys. Separate CF runtime (`*-runner-sa`) and invoker (`*-invoker-sa`) SAs; never use `google_project_iam_binding` in `02-api-connector.tf`.
- **No broad project roles on CF SAs:** Never grant project-wide `serviceAccountUser`, `run.invoker`, `cloudfunctions.invoker`, or `secretmanager.secretAccessor` to CF SAs. Bind secrets per-secret (`google_secret_manager_secret_iam_member`) and `serviceAccountUser` per-SA (`google_service_account_iam_member`).
- **Fail-closed `api-connector`:** Enqueued Cloud Tasks must include an `OidcToken` (`TASK_INVOKER_SA`). Both handlers must enforce server-side `ALLOWED_WORKFLOWS` (`src/workflow_policy.py`), validate `queue_name` (`ALLOWED_QUEUE_PREFIX`), `result_table` (`dataset.table`), and DNS-resolved public HTTPS targets (`validate_resolved_https_url`), strip cloud-metadata/hop-by-hop headers (`Metadata-Flavor`, `Host`, `Proxy-Authorization`, and `Authorization` when authenticated) while rejecting CRLF headers, set `allow_redirects=False`, avoid echoing upstream response bodies to HTTP callers, and hide internal errors.

## What Newcomers Get Wrong

### Docs & Config Traps
- **No `variables.tf` / `.tfvars`:** `1-foundations/README.md` and `docs/gdp-prerequisites.md` retain stale Fabric docs; real config is `config/<workspace>/{config.yml,workflows.json,backend.conf}`. (`docs/gdp-architeture.md` is misspelled, breaking links.)
- **Two workflow JSONs:** `config/<workspace>/workflows.json` drives DAGs, `ALLOWED_WORKFLOWS`, and postprocessing CFs; `config/api-connector/config.json` is uploaded separately to GCS (not workspace-scoped).
- **Auth docs contradict code:** `docs/gdp-workflow-config.md` lists `PUBLIC` and puts `auth_server`/`client_id` in Secret Manager. Code (`auth_type.py`, `cloud_task.py`) only has `CLIENT_CREDENTIALS`, `HTTP_BASIC`, and `NONE`: `CLIENT_CREDENTIALS` reads `auth_server`/`client_id` from `auth` JSON, expects only `{"client_secret": "..."}` in Secret Manager, and sends `Authorization: access_token <token>` (not `Bearer`). Omitting `auth` crashes `generic-api-service.py` even though `cloud_task.py` accepts `NONE` without `auth`.
- **Stale `api-connector/README.md` `curl`:** Omits `workflow_id` (arg 1 of 8 — see `_make_call_row()` in `tests/test_api_connector_security.py`).
- **No root `.gitignore`:** Only `api-connector/.gitignore` exists — `unittest` without `-B`, `terraform init`, or `venv/` elsewhere leaves untracked files.

### Cross-File Contracts
- **8-argument BQ routine order:** Keep exact order synced across `02-api-connector.tf`, `airflow/dags_template/generic-api-service.py`, and `api-connector/src/handlers/bigquery.py`: `workflow_id, request_config, auth, headers, query_string, body, result_table, queue_name`.
- **`tbl_process_log` ↔ `tbl_result` swap:** The DAG passes `tbl_process_log_<ts>` as `result_table`; `bigquery.py` and `cloud_task.py` swap prefixes via `.replace()`. Never rename either prefix independently.
- **Postprocessing activation & topic mapping:** `locals.tf` provisions a topic + CF only when a `workflows.json` `name` matches a non-empty `workflow-postprocess/<name>/` dir; `02-api-connector.tf` strips `${resource-prefix}-` and `-trf-ps-0` from topic names to populate `PUBSUB_TOPICS`.
- **DAG template (`generic-api-service.py`):** Escape literal `${...}` as `$${...}` for `templatefile()`. Never leave both `static_data` and `dynamic_data` empty for `headers`, `query_string`, or `body` (`_json_pairs()` emits invalid SQL `CONCAT('{',,'}')`).

### Existing Quirks & Bugs
- **Hardcoded `"US"` BQ location:** `google_bigquery_connection` (`02-api-connector.tf`) and `BigQueryCreateEmptyTableOperator` (`generic-api-service.py`) ignore `local.config.location` / `BQ_LOCATION`.
- **`apply.sh`:** `#!/bin/bash` is below the license header (run with `bash apply.sh`), the required-file loop checks undefined `${file_paths[@]}` instead of `${REQUIRED_CONFIG_FILES[@]}`, and `plan` uses `-lock=false`.
- **Multi-workflow bucket collision:** `transf-0-api-fnc` (`06-transformation.tf`) shares `bucket_name = "wfpp-src-${random_id.default.hex}"` across `for_each` — a second active workflow collides without unique/external buckets.
- **TF IAM & deletion gaps:** `drop-project.iam` (`01-dropoff.tf`) duplicates `"roles/storage.objectAdmin"` (overwriting `load-sa-df-0`); `dwh-{cur,conf,plg}` omit `deletion_policy = "ABANDON"`.
- **CF runtime traps:** `src/config.py` requires `PROJECT_ID` at import time; handlers fail closed without `ALLOWED_WORKFLOWS` (plus `REGION`, `FUNCTION_NAME`, `TASK_INVOKER_SA` for enqueueing). Run via `functions-framework --target main` (not `python main.py`). For `CLOUD_TASK`, `Handler.execute()` propagates `400`/`403`/`422`/`503` and turns executed results (including internal `500`s) into `202` to stop Cloud Tasks retries; `cloud_task.py` sends `body` via `requests` `data=` (not `json=`); `workflow1/main.py` uses `@functions_framework.http` (not `.cloud_event`).
