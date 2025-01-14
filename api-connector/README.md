# API Connector

The API (Workflow) Connector serves as glue logic between Airflow, BigQuery and Cloud Tasks. It is responsible for dispatching workflows so that API calls listed in [config.json](../config/api-connector/config.json) are sent to a Cloud Task queue, triggered, and their responses persisted in BigQuery.

## Quickstart

```sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

PROJECT_ID=<PROJECT_ID> functions-framework --target main --debug
```

## Authentication Types

### CLIENT_CREDENTIALS

Use the following auth parameter in your request:

```json
{
  "auth": {
    "type": "CLIENT_CREDENTIALS",
    "auth_server": "<AUTH_SERVER>",
    "client_id": "<CLIENT_ID>",
    "secret_name": "projects/<PROJECT_NUMBER>/secrets/<SECRET_NAME>/versions/<VERSION>"
  }
}
```

A Google Cloud Secret is expected to contain the following:

```json
{
  "client_secret": "<USERNAME>"
}
```

### HTTP_BASIC

Use the following auth parameter in your request:

```json
{
  "auth": {
    "type": "HTTP_BASIC",
    "secret_name": "projects/<PROJECT_NUMBER>/secrets/<SECRET_NAME>/versions/<VERSION>"
  }
}
```

A Google Cloud Secret is expected to contain the following:

```json
{
  "username": "<USERNAME>",
  "password": "<PASSWORD>"
}
```

## BigQuery Routine

```sh
curl -X POST localhost:8080 \
  -H "Content-Type: application/json" \
  -d '{
    "calls": [
      [
        "{\"uri\": \"https://pokeapi.co/api/v2/pokemon/ditto\", \"method\": \"GET\"}",
        "{\"type\": \"HTTP_BASIC\", \"secret_name\": \"projects/673200389551/secrets/securesm/versions/latest\"}",
        "{\"KEY\": \"api1234\", \"Content-Type\": \"application/json\"}",
        "{\"q1\": \"query1val2\"}",
        "{\"key1\": \"body1val2\"}",
        "gdp_cm_test3_dwh_load_bq_0.tbl_result_20241205182331",
        "projects/gdp-cm-test3-lod/locations/us-west1/queues/cloud-task-api-20241205182331"
      ]
    ]
  }'
```

## Cloud Task

```sh
curl -X POST localhost:8080 \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "workflow1",
    "request_config": {"uri": "https://pokeapi.co/api/v2/pokemon/ditto", "method": "GET"},
    "headers": {"KEY": "api1234", "Content-Type": "application/json"},
    "query_string": "q1=query1val2",
    "auth": {"type": "HTTP_BASIC", "secret_name": "projects/673200389551/secrets/securesm/versions/latest"},
    "body": {"key1": "body1val2"},
    "result_table": "gdp_cm_test3_dwh_load_bq_0.tbl_result_20241209211309",
    "source": "CLOUD_TASK"
  }'
```
