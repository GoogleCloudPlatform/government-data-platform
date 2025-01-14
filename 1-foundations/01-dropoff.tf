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

# tfdoc:file:description drop off project and resources.

locals {
  drop_orch_service_accounts = [
    module.load-sa-df-0.iam_email, module.orch-sa-cmp-0.iam_email
  ]
}

module "drop-project" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"

  deletion_policy = "ABANDON"
  parent          = "folders/${local.config.folder-id}"
  billing_account = local.config.billing-account
  prefix          = local.config.resource-prefix
  name            = "drp${local.project_suffix}"
  iam_by_principals = {
    "group:${local.groups.data-engineers}" = [
      "roles/bigquery.dataEditor",
      "roles/pubsub.editor",
      "roles/storage.admin",
    ]
  }
  iam = {
    "roles/bigquery.dataEditor" = [module.drop-sa-bq-0.iam_email]
    "roles/bigquery.user"       = [module.load-sa-df-0.iam_email]
    "roles/pubsub.publisher"    = [module.drop-sa-ps-0.iam_email]
    "roles/pubsub.subscriber" = concat(
      local.drop_orch_service_accounts, [module.load-sa-df-0.iam_email]
    )
    "roles/storage.objectAdmin"   = [module.load-sa-df-0.iam_email]
    "roles/storage.objectCreator" = [module.drop-sa-cs-0.iam_email]
    "roles/storage.objectAdmin"   = [module.orch-sa-cmp-0.iam_email]
    "roles/storage.admin"         = [module.load-sa-df-0.iam_email]
  }
  services = concat(local.config.core-services, [
    "bigquery.googleapis.com",
    "bigqueryreservation.googleapis.com",
    "bigquerystorage.googleapis.com",
    "cloudkms.googleapis.com",
    "pubsub.googleapis.com",
    "storage.googleapis.com",
    "storage-component.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "compute.googleapis.com",
    "servicemanagement.googleapis.com",
    "apigateway.googleapis.com",
    "servicecontrol.googleapis.com",
    "orgpolicy.googleapis.com",

  ])

  /*service_encryption_key_ids = {
    "bigquery.googleapis.com" = [try(local.service_encryption_keys.bq, null)]
    "pubsub.googleapis.com"   = [try(local.service_encryption_keys.pubsub, null)]
    "storage.googleapis.com"  = [try(local.service_encryption_keys.storage, null)]
  }*/
}

# Cloud Storage

module "drop-sa-cs-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"

  project_id   = module.drop-project.project_id
  prefix       = local.config.resource-prefix
  name         = "drp-cs-0"
  display_name = "Data platform GCS drop off service account."
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [
      local.groups_iam.data-engineers
    ]
  }
}

module "drop-cs-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v36.0.1"

  project_id     = module.drop-project.project_id
  prefix         = local.config.resource-prefix
  name           = "drp-cs-0"
  location       = local.config.region
  storage_class  = "REGIONAL"
  encryption_key = try(local.service_encryption_keys.storage, null)
  force_destroy  = local.config.force-destroy
}

# PubSub

module "drop-sa-ps-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"

  project_id   = module.drop-project.project_id
  prefix       = local.config.resource-prefix
  name         = "drp-ps-0"
  display_name = "Data platform PubSub drop off service account"
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [
      local.groups_iam.data-engineers
    ]
  }
}

module "drop-ps-0" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v36.0.1"
  project_id = module.drop-project.project_id
  name       = "${local.config.resource-prefix}-drp-ps-0"
  kms_key    = try(local.service_encryption_keys.pubsub, null)
}

# BigQuery

module "drop-sa-bq-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"

  project_id   = module.drop-project.project_id
  prefix       = local.config.resource-prefix
  name         = "drp-bq-0"
  display_name = "Data platform BigQuery drop off service account"
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [local.groups_iam.data-engineers]
  }
}

module "drop-bq-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v36.0.1"

  project_id     = module.drop-project.project_id
  id             = "${replace(local.config.resource-prefix, "-", "_")}_drp_bq_0"
  location       = local.config.location
  encryption_key = try(local.service_encryption_keys.bq, null)
}
