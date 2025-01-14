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

# tfdoc:file:description Trasformation project and VPC.

locals {
  transf_subnet = (
    local.use_shared_vpc
    ? local.config.network_config.subnet_self_links.transformation
    : values(module.transf-vpc.0.subnet_self_links)[0]
  )
  transf_vpc = (
    local.use_shared_vpc
    ? local.config.network_config.network_self_link
    : module.transf-vpc.0.self_link
  )
}

module "transf-project" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"

  deletion_policy = "ABANDON"
  parent          = "folders/${local.config.folder-id}"
  billing_account = local.config.billing-account
  prefix          = local.config.resource-prefix
  name            = "trf${local.project_suffix}"
  iam_by_principals = {
    "group:${local.groups.data-engineers}" = [
      "roles/bigquery.jobUser",
      "roles/dataflow.admin",
    ]
  }
  iam = {
    "roles/bigquery.jobUser" = [
      module.transf-sa-bq-0.iam_email
    ]
    "roles/dataflow.admin" = [
      module.orch-sa-cmp-0.iam_email
    ]
    "roles/dataflow.worker" = [
      module.transf-sa-df-0.iam_email
    ]
    "roles/storage.objectAdmin" = [
      module.transf-sa-df-0.iam_email,
      "serviceAccount:${module.transf-project.service_agents.dataflow.email}"
    ]
  }
  services = concat(local.config.core-services, [
    "bigquery.googleapis.com",
    "bigqueryreservation.googleapis.com",
    "bigquerystorage.googleapis.com",
    "cloudkms.googleapis.com",
    "compute.googleapis.com",
    "dataflow.googleapis.com",
    "dlp.googleapis.com",
    "pubsub.googleapis.com",
    "servicenetworking.googleapis.com",
    "storage.googleapis.com",
    "storage-component.googleapis.com",
    "orgpolicy.googleapis.com",
    "run.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudbuild.googleapis.com",
    "eventarc.googleapis.com"
  ])
  /*service_encryption_key_ids = {
    "dataflow.googleapis.com" = [try(local.service_encryption_keys.dataflow, null)]
    "storage.googleapis.com"  = [try(local.service_encryption_keys.storage, null)]
  }*/
  shared_vpc_service_config = local.shared_vpc_project == null ? null : {
    attach       = true
    host_project = local.shared_vpc_project
  }
}

# Cloud Storage

module "transf-sa-df-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"

  project_id   = module.transf-project.project_id
  prefix       = local.config.resource-prefix
  name         = "trf-df-0"
  display_name = "Data platform Dataflow transformation service account"
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [
      local.groups_iam.data-engineers,
      module.orch-sa-cmp-0.iam_email
    ],
    "roles/iam.serviceAccountUser" = [
      module.orch-sa-cmp-0.iam_email
    ]
  }
}

module "transf-cs-df-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v36.0.1"

  project_id     = module.transf-project.project_id
  prefix         = local.config.resource-prefix
  name           = "trf-cs-0"
  location       = local.config.region
  storage_class  = "REGIONAL"
  encryption_key = try(local.service_encryption_keys.storage, null)
}

# BigQuery

module "transf-sa-bq-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"

  project_id   = module.transf-project.project_id
  prefix       = local.config.resource-prefix
  name         = "trf-bq-0"
  display_name = "Data platform BigQuery transformation service account"
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [
      local.groups_iam.data-engineers,
      module.orch-sa-cmp-0.iam_email
    ],
    "roles/iam.serviceAccountUser" = [
      module.orch-sa-cmp-0.iam_email
    ]
  }
}

# internal VPC resources

module "transf-vpc" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc?ref=v36.0.1"

  count      = local.use_shared_vpc ? 0 : 1
  project_id = module.transf-project.project_id
  name       = "${local.config.resource-prefix}-default"
  subnets = [
    {
      ip_cidr_range = "10.10.0.0/24"
      name          = "default"
      region        = local.config.region
    }
  ]
}

module "transf-vpc-firewall" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall?ref=v36.0.1"
  count      = local.use_shared_vpc ? 0 : 1
  project_id = module.transf-project.project_id
  network    = module.transf-vpc.0.name
  default_rules_config = {
    admin_ranges = ["10.10.0.0/24"]
  }
}

module "transf-nat" {
  source         = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v36.0.1"
  count          = local.use_shared_vpc ? 0 : 1
  project_id     = module.transf-project.project_id
  name           = "${local.config.resource-prefix}-default"
  region         = local.config.region
  router_network = module.transf-vpc.0.name
}

resource "google_service_account" "transf-0-api-fnc-invoker-sa" {
  project      = module.transf-project.project_id
  account_id   = "wfpp-fnc-sa-invoker"
  display_name = "A service account to invoke the load API function"
}

resource "google_project_iam_member" "transf-0-api-fnc-invoker-sa-iam" {
  for_each = toset([
    "roles/iam.serviceAccountUser",
    "roles/cloudfunctions.invoker",
    "roles/run.invoker"
  ])
  project = module.transf-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.transf-0-api-fnc-invoker-sa.email}"
}

resource "google_project_iam_member" "load-transf-0-api-fnc-invoker-sa-iam" {
  for_each = toset([
    "roles/iam.serviceAccountUser",
    "roles/cloudfunctions.invoker",
    "roles/run.invoker"
  ])
  project = module.transf-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"
}

# Workflow postprocessing queue
module "transf-ps-0" {
  for_each = toset(local.active-workflows)

  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v36.0.1"
  project_id = module.transf-project.project_id
  name       = "${local.config.resource-prefix}-${each.key}-trf-ps-0"
  kms_key    = try(local.service_encryption_keys.pubsub, null)
}

# Workflow postprocessing functions
module "transf-0-api-fnc" {
  depends_on = [module.transf-project, google_service_account.transf-0-api-fnc-invoker-sa]

  for_each = toset(local.active-workflows)

  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  project_id       = module.transf-project.project_id
  name             = "workflow-postprocess-${each.key}"
  region           = local.config.region
  ingress_settings = "ALLOW_ALL"
  bucket_name      = "wfpp-src-${random_id.default.hex}"
  bucket_config = {
    path                      = "workflow-postprocess/"
    force_destroy             = true
    lifecycle_delete_age_days = 1
  }
  bundle_config = {
    path = "../workflow-postprocess/${each.key}/"
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
    PROJECT_NUMBER = module.transf-project.number
    PROJECT_ID     = module.transf-project.project_id
    FUNCTION_NAME  = "workflow-postprocess-${each.key}"
    REGION         = local.config.region
  }

  trigger_config = {
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = module.transf-ps-0[each.key].topic.id
    service_account_email = google_service_account.transf-0-api-fnc-invoker-sa.email
  }

  service_account        = google_service_account.transf-0-api-fnc-invoker-sa.email
  service_account_create = false
}

// TODO: Trocar destino da function de load para landing
