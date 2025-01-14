# API Processing DAGs

The [dags_template](./dags_template/) folder contains a template DAG that dynamically resolves to DAGs responsible for calling a BigQuery procedure that triggers the [api-connector](../api-connector/) and kickstarts a series of API calls to endpoints specified in [config.json](../config/api-connector/config.json) and parametrized by [sample.csv](../config/api-connector/sample.csv).

## Run a local Airflow environment with Composer Local Development CLI tool

This section aims to describe how to create, configure, and run a local Airflow environment using the Composer Local Development CLI tool.

Check to see if this [guide](https://cloud.google.com/composer/docs/composer-3/run-local-airflow-environments) is available.
