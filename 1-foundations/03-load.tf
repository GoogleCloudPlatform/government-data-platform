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

# tfdoc:file:description Load project and VPC.

locals {
  load_service_accounts = [
    "serviceAccount:${module.load-project.service_agents.dataflow.email}",
    module.load-sa-df-0.iam_email
  ]
  load_subnet = (
    local.use_shared_vpc
    ? local.config.network_config.subnet_self_links.load
    : values(module.load-vpc.0.subnet_self_links)[0]
  )
  load_vpc = (
    local.use_shared_vpc
    ? local.config.network_config.network_self_link
    : module.load-vpc.0.self_link
  )
}

# Project

module "load-project" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"

  deletion_policy = "ABANDON"
  parent          = "folders/${local.config.folder-id}"
  billing_account = local.config.billing-account
  prefix          = local.config.resource-prefix
  name            = "lod${local.project_suffix}"
  iam_by_principals = {
    "group:${local.groups.data-engineers}" = [
      "roles/compute.viewer",
      "roles/dataflow.admin",
      "roles/dataflow.developer",
      "roles/viewer",
    ]
  }
  iam = {
    "roles/bigquery.jobUser" = [module.load-sa-df-0.iam_email]
    "roles/dataflow.admin" = [
      module.orch-sa-cmp-0.iam_email, module.load-sa-df-0.iam_email
    ]
    "roles/dataflow.worker"     = [module.load-sa-df-0.iam_email]
    "roles/storage.objectAdmin" = local.load_service_accounts
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
    "cloudbuild.googleapis.com",
    "orgpolicy.googleapis.com",
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "cloudtasks.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  /*service_encryption_key_ids = {
    "pubsub.googleapis.com"   = [try(local.service_encryption_keys.pubsub, null)]
    "dataflow.googleapis.com" = [try(local.service_encryption_keys.dataflow, null)]
    "storage.googleapis.com"  = [try(local.service_encryption_keys.storage, null)]
  }*/
  shared_vpc_service_config = local.shared_vpc_project == null ? null : {
    attach       = true
    host_project = local.shared_vpc_project
  }
}

resource "google_service_account" "load-cmp-sa-0" {
  depends_on   = [module.load-project]
  project      = module.load-project.project_id
  account_id   = "load-cmp-sa-0"
  display_name = "SA used start data flow using Composer"
}

resource "google_project_iam_member" "load-cmp-sa-0-iam" {
  for_each = toset([
    "roles/iam.serviceAccountTokenCreator",
    "roles/bigquery.admin",
    "roles/cloudtasks.queueAdmin",
    "roles/cloudtasks.viewer",
    "roles/storage.admin",
  ])
  project = module.load-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.load-cmp-sa-0.email}"
}

resource "google_service_account_iam_member" "load-cmp-sa-0-iam-orch" {
  service_account_id = google_service_account.load-cmp-sa-0.id
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${module.orch-sa-cmp-0.email}"
}

module "load-sa-df-0" {
  source       = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"
  project_id   = module.load-project.project_id
  prefix       = local.config.resource-prefix
  name         = "load-df-0"
  display_name = "Data platform Dataflow load service account"
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [local.groups_iam.data-engineers, module.orch-sa-cmp-0.iam_email]
    "roles/iam.serviceAccountUser"         = [module.orch-sa-cmp-0.iam_email]
  }
}

module "load-cs-df-0" {
  source         = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v36.0.1"
  project_id     = module.load-project.project_id
  prefix         = local.config.resource-prefix
  name           = "load-cs-0"
  location       = local.config.region
  storage_class  = "REGIONAL"
  encryption_key = try(local.service_encryption_keys.storage, null)
}

# internal VPC resources

module "load-vpc" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc?ref=v36.0.1"

  count      = local.use_shared_vpc ? 0 : 1
  project_id = module.load-project.project_id
  name       = "${local.config.resource-prefix}-default"
  subnets = [
    {
      ip_cidr_range = "10.10.0.0/24"
      name          = "default"
      region        = local.config.region
    }
  ]
}

module "load-vpc-firewall" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall?ref=v36.0.1"

  count      = local.use_shared_vpc ? 0 : 1
  project_id = module.load-project.project_id
  network    = module.load-vpc.0.name
  default_rules_config = {
    admin_ranges = ["10.10.0.0/24"]
  }
}

module "load-nat" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v36.0.1"

  count          = local.use_shared_vpc ? 0 : 1
  project_id     = module.load-project.project_id
  name           = "${local.config.resource-prefix}-default"
  region         = local.config.region
  router_network = module.load-vpc.0.name
}

module "sa-udf-bq" {
  source       = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"
  project_id   = module.transf-project.project_id
  name         = "sa-udf-bq"
  display_name = "Data platform Cloud Function transformation service account"

  iam_project_roles = {
    (module.load-project.project_id) = [
      "roles/cloudfunctions.invoker",
      "roles/storage.objectCreator",
      "roles/storage.objectViewer",
      "roles/secretmanager.secretAccessor",
    ]
  }
}
