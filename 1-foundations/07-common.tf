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

# tfdoc:file:description common project.

module "common-project" {
  source          = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"
  deletion_policy = "ABANDON"

  parent          = "folders/${local.config.folder-id}"
  billing_account = local.config.billing-account
  prefix          = local.config.resource-prefix
  name            = "cmn${local.project_suffix}"
  iam_by_principals = {
    "group:${local.groups.data-analysts}" = [
      "roles/datacatalog.viewer",
    ]
    "group:${local.groups.data-engineers}" = [
      "roles/dlp.reader",
      "roles/dlp.user",
      "roles/dlp.estimatesAdmin",
    ]
    "group:${local.groups.data-security}" = [
      "roles/dlp.admin",
      "roles/datacatalog.admin"
    ]
  }
  iam = {
    "roles/dlp.user" = [
      module.load-sa-df-0.iam_email,
      module.transf-sa-df-0.iam_email
    ]
    "roles/datacatalog.viewer" = [
      module.load-sa-df-0.iam_email,
      module.transf-sa-df-0.iam_email,
      module.transf-sa-bq-0.iam_email
    ]
    "roles/datacatalog.categoryFineGrainedReader" = [
      module.transf-sa-df-0.iam_email,
      module.transf-sa-bq-0.iam_email,
      # Uncomment if you want to grant access to `data-analyst` to all columns tagged.
      # local.groups_iam.data-analysts
    ]
  }
  services = concat(local.config.core-services, [
    "datacatalog.googleapis.com",
    "dlp.googleapis.com",
    "orgpolicy.googleapis.com",
  ])
}

# Data Catalog Policy tag

module "common-datacatalog" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/data-catalog-policy-tag?ref=v36.0.1"

  project_id = module.common-project.project_id
  name       = "${local.config.resource-prefix}-datacatalog-policy-tags"
  location   = local.config.location
  tags       = local.config.data-catalog.tags
}

# To create KMS keys in the common projet: uncomment this section and assigne key links accondingly in local.service_encryption_keys variable

# module "cmn-kms-0" {
#   source     = "../modules/kms"
#   project_id = module.common-project.project_id
#   keyring = {
#     name     = "${local.config.resource-prefix}-kr-global",
#     location = "global"
#   }
#   keys = {
#     pubsub = null
#   }
# }

# module "cmn-kms-1" {
#   source     = "../modules/kms"
#   project_id = module.common-project.project_id
#   keyring = {
#     name     = "${local.config.resource-prefix}-kr-mregional",
#     location = local.config.location
#   }
#   keys = {
#     bq      = null
#     storage = null
#   }
# }

# module "cmn-kms-2" {
#   source     = "../modules/kms"
#   project_id = module.cmn-prj.project_id
#   keyring = {
#     name     = "${local.config.resource-prefix}-kr-regional",
#     location = local.config.region
#   }
#   keys = {
#     composer = null
#     dataflow = null
#   }
# }
