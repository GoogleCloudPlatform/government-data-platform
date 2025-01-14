# Workflow Configuration

This guide explains how to configure a workflow using the provided JSON file.

---

## Overview

The JSON defines workflows that process data, interact with APIs, and manage response handling. Each workflow includes:

1. **`ingestion_type`**: Must be `airflow_gcs_to_bigquery` string.
2. **`airflow_gcs_to_bigquery_config`**  

This configuration defines the settings for using `GCSToBigQueryOperator`. It allows customization for loading data from GCS into BigQuery.  


- You can refer to the [official Airflow Operator documentation](https://airflow.apache.org/docs/) to add or modify additional configurations.  
- Note: The `destination_project_dataset_table` configuration will be **overridden** if explicitly set in this workflow.

3. **`airflow_dag_config`**: 
Containing DAG properties.

See [Parameters]( https://airflow.apache.org/docs/apache-airflow/stable/_api/airflow/models/dag/index.html) section in official Airflow documentation


3. **API Calls**: Interact with external APIs.
4. **Response Processing**: Prepare responses for further use.

---

## Key Sections

### 1. Workflow Basics
- **`name`**: Workflow name (e.g., `"workflow1"`).
- **`ingestion_type`**: Specifies the type of ingestion (e.g., `"airflow_gcs_to_bigquery"`).

---

### 2. Data Ingestion (`airflow_gcs_to_bigquery_config`)
- **Bucket**: Source bucket path (e.g., `"gs://gdp-cm-test3-load-cs-0"`).
- **File**: Name of the file to process (e.g., `"sample.csv"`).
- **Schema**: Defines fields for BigQuery:
  - Example: `api-key`, `query1`, `query2`, `body1`.

---

### 3. Scheduling (`airflow_dag_config`)
- **Schedule**: When the workflow runs (e.g., `"*/15 * * * *"` for every 15 minutes).

---

### 4. API Configuration (`api_config`)

#### Authentication

The `auth` block allows you to configure the `type` for API authentication purposes.

The sensitive data is stored in the `secret_name`, which is the secret resource name in the format `projects/<project-number>/secrets/<secret-id>/versions/latest`

The supported `type` are: 

`PUBLIC`

API does not require an authentication method. This is the default setting.

`CLIENT_CREDENTIALS`

You nees store in Secret Manager a JSON with bellow format:
```json
{
    "auth_server":"url to request an access token", 
    "client_id":"client-id-value",
    "client_secret": "super-secret-client-id"
}
```

`HTTP_BASIC`: Basic authentication is a simple authentication scheme built into the HTTP protocol. The client sends HTTP requests with the Authorization header that contains the word Basic word followed by a space and a base64-encoded string username:password

You nees store in Secret Manager a JSON with bellow format:

```json
{
    "username":"apiuser",
    "password":"supersecret"
}
```

Make sure to assign the IAM role `roles/secretmanager.secretAccessor` to allow the service account to read the secret.

```ssh
gcloud secrets add-iam-policy-binding [SECRET_ID] \
    --member="serviceAccount:[SERVICE_ACCOUNT_EMAIL]" \
    --role="roles/secretmanager.secretAccessor"
```
To retrieve the SERVICE_ACCOUNT_EMAIL, run this command after creating all the resources.
By default, this permission is already set for all secrets residing in the `load` project.
```
terraform output load-0-api-load-runner-sa
```

Or add this permission using Terraform:

```hcl
resource "google_secret_manager_secret_iam_member" "member" {
  project = "" #The ID of the project in which the resource belongs.
  secret_id = "<secret-id>" #Secret ID
  role = "roles/secretmanager.secretAccessor"
  member = "serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"
}
```


- **``Type``**: E.g., `"HTTP_BASIC"`.
- **Secret**: Path to secret (e.g., `"projects/.../secrets/..."`).

#### Request
- **URL**: API endpoint (e.g., `"https://pokeapi.co/api/v2/pokemon/ditto"`).
- **Timeout**: Request timeout in seconds.
- **Method**: HTTP method (e.g., `"GET"`).
- **Dynamic Data**:
  - Use fields from the data (e.g., `api-key`, `query1`).

#### Response
- **Format**: Expected format (e.g., `"JSON"`).

---

### 5. Response Processing (`process_response`)
- **Dataform (Optional)**:
  - Can handle post-processing but is disabled here (`"enable": "false"`).

---

## Example Workflow

1. **Data Source**: A CSV file (`sample.csv`) in GCS.
2. **Ingestion**: Data is loaded to BigQuery with a custom schema.
3. **API Call**: A request to fetch external API data.
4. **Processing**: The data is processed and transformed to prepare the response for seamless integration with external systems.

---

## Tips for Developers

1. **Add Workflows**: To create new workflows, copy and adjust the structure.
2. **Use Secrets**: Keep sensitive info (e.g., API keys) in secret managers.
3. **Test**: Run workflows in a test environment before deploying.

---