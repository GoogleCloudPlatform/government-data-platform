# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

resource "google_bigquery_connection" "load-connection-bq-0" {
  depends_on    = [module.load-project]
  project       = module.load-project.project_id
  connection_id = "load-connection-bq-0"
  location      = "US"
  cloud_resource {}
}

resource "google_project_iam_binding" "load-0-grant-bq-connection-run-invoker" {
  depends_on = [module.load-project]

  for_each = toset([
    "roles/cloudfunctions.invoker",
    "roles/run.invoker",
  ])
  project = module.load-project.project_id
  role    = each.key
  members = [
    "serviceAccount:${google_bigquery_connection.load-connection-bq-0.cloud_resource[0].service_account_id}"
  ]
}

resource "google_service_account" "load-0-api-sa" {
  depends_on   = [module.load-project]
  project      = module.load-project.project_id
  account_id   = "load-0-api-sa"
  display_name = "SA used to API load data"
}

resource "google_project_iam_member" "load-0-api-sa-iam" {
  for_each = toset([
    "roles/iam.serviceAccountUser",
    "roles/bigquery.admin",
    "roles/cloudtasks.queueAdmin",
    "roles/cloudtasks.viewer",
    "roles/secretmanager.secretAccessor"
  ])
  project = module.load-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.load-0-api-sa.email}"
}

resource "google_bigquery_routine" "dwh-load-bq-remote-fnc-0" {
  depends_on      = [google_service_account.load-0-api-sa, module.dwh-load-bq-0, module.load-0-api-fnc]
  project         = module.load-project.project_id
  dataset_id      = module.dwh-load-bq-0.dataset_id
  routine_id      = "routine_execute_api_fnc"
  routine_type    = "SCALAR_FUNCTION"
  definition_body = ""

  arguments {
    name      = "workflow_id"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "request_config"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "auth"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "headers"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "query_string"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "body"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "result_table"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  arguments {
    name      = "queue_name"
    data_type = "{\"typeKind\" :  \"STRING\"}"
  }

  return_type = jsonencode({
    typeKind : "JSON"
  })

  remote_function_options {
    max_batching_rows = "10"
    endpoint          = module.load-0-api-fnc.uri
    connection        = google_bigquery_connection.load-connection-bq-0.name
  }
}

resource "random_id" "default" {
  byte_length = 8
}

resource "google_service_account" "load-0-api-fnc-runner-sa" {
  project      = module.load-project.project_id
  account_id   = "api-load-runner-sa"
  display_name = "A service account to run the load API function"
}

output "load-0-api-load-runner-sa" {
  value = google_service_account.load-0-api-fnc-runner-sa.email
}

resource "google_service_account" "data-workflow-trigger-api-fnc-runner-sa" {
  project      = module.load-project.project_id
  account_id   = "workflow-trigger-api-fnc-sa"
  display_name = "A service account to run the data-workflow-trigger API function"
}

resource "google_project_iam_member" "load-0-api-fnc-runner-sa-iam" {
  for_each = toset([
    "roles/cloudtasks.enqueuer",
    "roles/iam.serviceAccountUser",
    "roles/secretmanager.secretAccessor",
    "roles/bigquery.dataEditor",
  ])
  project = module.load-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"
}


resource "google_service_account" "load-0-api-fnc-invoker-sa" {
  project      = module.load-project.project_id
  account_id   = "api-src-fnc-sa-invoker"
  display_name = "A service account to invoke the load API function"
}

resource "google_project_iam_member" "load-0-api-fnc-invoker-sa-iam" {
  for_each = toset([
    "roles/iam.serviceAccountUser",
    "roles/cloudfunctions.invoker",
    "roles/run.invoker"
  ])
  project = module.load-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.load-0-api-fnc-invoker-sa.email}"
}

resource "google_storage_bucket_object" "load-config-for-templated-dag" {
  depends_on   = [module.load-cs-df-0]
  name         = "api-connector/config.json"
  bucket       = module.load-cs-df-0.name
  source       = "${path.root}/../config/api-connector/config.json"
  content_type = "application/json; charset=UTF-8"
}

module "load-0-api-fnc" {
  depends_on = [module.load-project, google_service_account.load-0-api-fnc-runner-sa]

  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  project_id       = module.load-project.project_id
  name             = "load-0-api-fnc"
  region           = local.config.region
  ingress_settings = "ALLOW_ALL"
  bucket_name      = "api-src-${random_id.default.hex}"
  bucket_config = {
    path                      = "load-process/"
    force_destroy             = true
    lifecycle_delete_age_days = 1
  }
  bundle_config = {
    path = "../api-connector/"
  }

  iam = {
    "roles/run.invoker" = ["allUsers"]
  }

  function_config = {
    entry_point    = "main"
    instance_count = 100
    cpu            = 1
    memory         = 128
    runtime        = "python310"
    timeout        = 480 # Timeout in seconds, increase it if your CF timeouts.
  }
  environment_variables = {
    PROJECT_NUMBER  = module.load-project.number
    PROJECT_ID      = module.load-project.project_id
    FUNCTION_NAME   = "load-0-api-fnc"
    REGION          = local.config.region
    PUBSUB_TOPICS = jsonencode([for ps in module.transf-ps-0 : {
      replace(replace(ps.topic.name, "${local.config.resource-prefix}-", ""), "-trf-ps-0", "") = ps.topic.id
    }])
  }

  service_account        = google_service_account.load-0-api-fnc-runner-sa.email
  service_account_create = false
}
