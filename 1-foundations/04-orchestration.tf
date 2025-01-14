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

# tfdoc:file:description Orchestration project and VPC.

locals {
  orch_subnet = (
    local.use_shared_vpc
    ? local.config.network_config.subnet_self_links.orchestration
    : values(module.orch-vpc.0.subnet_self_links)[0]
  )
  orch_vpc = (
    local.use_shared_vpc
    ? local.config.network_config.network_self_link
    : module.orch-vpc.0.self_link
  )
}

module "orch-project" {
  source          = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"
  deletion_policy = "ABANDON"
  parent          = "folders/${local.config.folder-id}"
  billing_account = local.config.billing-account
  prefix          = local.config.resource-prefix
  name            = "orc${local.project_suffix}"

  iam_by_principals = {
    "group:${local.groups.data-engineers}" = [
      "roles/bigquery.dataEditor",
      "roles/bigquery.jobUser",
      "roles/cloudbuild.builds.editor",
      "roles/composer.admin",
      "roles/composer.environmentAndStorageObjectAdmin",
      "roles/iap.httpsResourceAccessor",
      "roles/iam.serviceAccountUser",
      "roles/storage.objectAdmin",
      "roles/storage.admin",
    ]
  }
  iam = {
    "roles/bigquery.dataEditor" = [
      module.load-sa-df-0.iam_email,
      module.transf-sa-df-0.iam_email
    ]
    "roles/bigquery.jobUser" = [
      module.orch-sa-cmp-0.iam_email
    ]
    "roles/composer.worker" = [
      module.orch-sa-cmp-0.iam_email
    ]
    "roles/iam.serviceAccountTokenCreator" = [
      module.orch-sa-cmp-0.iam_email
    ]
    "roles/iam.serviceAccountUser" = [
      module.orch-sa-cmp-0.iam_email
    ]
    "roles/storage.objectAdmin" = [
      module.orch-sa-cmp-0.iam_email,
      "serviceAccount:${module.orch-project.service_agents.composer.email}"
    ]
    "roles/storage.objectViewer" = [
      module.load-sa-df-0.iam_email
    ]
  }


  services = concat(local.config.core-services, [
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "bigqueryreservation.googleapis.com",
    "bigquerystorage.googleapis.com",
    "cloudkms.googleapis.com",
    "composer.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "containerregistry.googleapis.com",
    "dataflow.googleapis.com",
    "orgpolicy.googleapis.com",
    "pubsub.googleapis.com",
    "servicenetworking.googleapis.com",
    "storage.googleapis.com",
    "storage-component.googleapis.com",
    "cloudbuild.googleapis.com",
    "orgpolicy.googleapis.com",
  ])

  # org_policies = {
  #   "constraints/compute.requireOsLogin" = {
  #     enforce = false
  #   }
  # }

  /*service_encryption_key_ids = {
    "composer.googleapis.com" = [try(local.service_encryption_keys.composer, null)]
    "storage.googleapis.com"  = [try(local.service_encryption_keys.storage, null)]
  }*/
  shared_vpc_service_config = local.shared_vpc_project == null ? null : {
    attach       = true
    host_project = local.shared_vpc_project
  }
}

# Cloud Storage

module "orch-cs-0" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v36.0.1"

  project_id     = module.orch-project.project_id
  prefix         = local.config.resource-prefix
  name           = "orc-cs-0"
  location       = local.config.region
  storage_class  = "REGIONAL"
  encryption_key = try(local.service_encryption_keys.storage, null)
}

# internal VPC resources

module "orch-vpc" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc?ref=v36.0.1"

  count      = local.use_shared_vpc ? 0 : 1
  project_id = module.orch-project.project_id
  name       = "${local.config.resource-prefix}-default"
  subnets = [
    {
      ip_cidr_range = "10.10.0.0/24"
      name          = "default"
      region        = local.config.region
      secondary_ip_ranges = {
        pods     = "10.10.8.0/22"
        services = "10.10.12.0/24"
      }
    }
  ]
}

module "orch-vpc-firewall" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall?ref=v36.0.1"

  count      = local.use_shared_vpc ? 0 : 1
  project_id = module.orch-project.project_id
  network    = module.orch-vpc.0.name
  default_rules_config = {
    admin_ranges = ["10.10.0.0/24"]
  }
}

module "orch-nat" {
  count  = local.use_shared_vpc ? 0 : 1
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v36.0.1"

  project_id     = module.orch-project.project_id
  name           = "${local.config.resource-prefix}-default"
  region         = local.config.region
  router_network = module.orch-vpc.0.name
}

module "orch-sa-cmp-0" {
  source       = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"
  project_id   = module.orch-project.project_id
  prefix       = local.config.resource-prefix
  name         = "orc-cmp-0"
  display_name = "Data platform Composer service account"
  iam = {
    "roles/iam.serviceAccountTokenCreator" = [local.groups_iam.data-engineers]
  }
}



# resource "google_project_iam_member" "orch-sa-cmp-0" {
#   for_each = toset([
#     "roles/iam.serviceAccountTokenCreator",
#     "roles/iam.serviceAccountUser",
#     "roles/composer.worker",
#     "roles/bigquery.jobUser"
#   ])
#   project = module.main_project.id
#   role =  each.key
#   member = "serviceAccount:${google_service_account.orc-cmp-sa-0.email}"
# }

resource "google_composer_environment" "orch-cmp-0" {

  # depends_on = [ google_service_account.load-cmp-sa-0 ]
  # count = 0
  provider = google-beta
  project  = module.orch-project.project_id
  name     = "orc-cmp-0"
  region   = local.config.region
  config {
    workloads_config {
      scheduler {
        cpu        = 0.5
        memory_gb  = 2
        storage_gb = 1
        count      = 1
      }
      triggerer {
        cpu       = 0.5
        memory_gb = 1
        count     = 1
      }
      dag_processor {
        cpu        = 1
        memory_gb  = 2
        storage_gb = 1
        count      = 1
      }
      web_server {
        cpu        = 0.5
        memory_gb  = 2
        storage_gb = 1
      }
      worker {
        cpu        = 0.5
        memory_gb  = 2
        storage_gb = 1
        min_count  = 1
        max_count  = 3
      }
    }

    environment_size = "ENVIRONMENT_SIZE_SMALL"


    node_config {
      service_account = module.orch-sa-cmp-0.email
      tags            = ["composer-worker", "http-server", "https-server"]
    }
    # private_environment_config {
    #   enable_private_endpoint = "true"
    #   cloud_sql_ipv4_cidr_block = try(
    #     local.config.network_config.composer_ip_ranges.cloudsql, "10.20.10.0/24"
    #   )
    #   master_ipv4_cidr_block = try(
    #     local.config.network_config.composer_ip_ranges.gke_master, "10.20.11.0/28"
    #   )
    #   web_server_ipv4_cidr_block = try(
    #     local.config.network_config.composer_ip_ranges.web_server, "10.20.11.16/28"
    #   )
    # }
    software_config {
      image_version = local.config.composer.airflow_version
      # pypi_packages = {
      #     PACKAGE_NAME = "EXTRAS_AND_VERSION"
      # }
      env_variables = merge(
        local.config.composer.env_variables, {
          BQ_LOCATION                 = local.config.location
          DATA_CAT_TAGS               = try(jsonencode(module.common-datacatalog.tags), "{}")
          DF_KMS_KEY                  = try(local.config.encryption-keys.dataflow, "")
          DWH_LAND_PRJ                = module.dwh-lnd-project.project_id
          DWH_LAND_BQ_DATASET         = module.dwh-lnd-bq-0.dataset_id
          DWH_LAND_GCS                = module.dwh-lnd-cs-0.url
          DWH_CURATED_PRJ             = module.dwh-cur-project.project_id
          DWH_CURATED_BQ_DATASET      = module.dwh-cur-bq-0.dataset_id
          DWH_CURATED_GCS             = module.dwh-cur-cs-0.url
          DWH_CONFIDENTIAL_PRJ        = module.dwh-conf-project.project_id
          DWH_CONFIDENTIAL_BQ_DATASET = module.dwh-conf-bq-0.dataset_id
          DWH_CONFIDENTIAL_GCS        = module.dwh-conf-cs-0.url
          DWH_PLG_PRJ                 = module.dwh-plg-project.project_id
          DWH_PLG_BQ_DATASET          = module.dwh-plg-bq-0.dataset_id
          DWH_PLG_GCS                 = module.dwh-plg-cs-0.url
          GCP_REGION                  = local.config.region
          LOD_PRJ                     = module.load-project.project_id
          LOD_SA                      = google_service_account.load-cmp-sa-0.email
          LOD_GCS_STAGING             = module.load-cs-df-0.url
          LOD_NET_VPC                 = local.load_vpc
          LOD_NET_SUBNET              = local.load_subnet
          LOD_BQ_DATASET              = module.dwh-load-bq-0.dataset_id
          ORC_PRJ                     = module.orch-project.project_id
          ORC_GCS                     = module.orch-cs-0.url
          TRF_PRJ                     = module.transf-project.project_id
          TRF_GCS_STAGING             = module.transf-cs-df-0.url
          TRF_NET_VPC                 = local.transf_vpc
          TRF_NET_SUBNET              = local.transf_subnet
          TRF_SA_DF                   = module.transf-sa-df-0.email
          TRF_SA_BQ                   = module.transf-sa-bq-0.email
        }
      )
    }

    # dynamic "encryption_config" {
    #   for_each = (
    #     try(local.service_encryption_keys.composer != null, false)
    #     ? { 1 = 1 }
    #     : {}
    #   )
    #   content {
    #     kms_key_name = try(local.service_encryption_keys.composer, null)
    #   }
    # }

    # dynamic "web_server_network_access_control" {
    #   for_each = toset(
    #     local.config.network_config.web_server_network_access_control == null
    #     ? []
    #     : [local.config.network_config.web_server_network_access_control]
    #   )
    #   content {
    #     dynamic "allowed_ip_range" {
    #       for_each = toset(web_server_network_access_control.key)
    #       content {
    #         value = allowed_ip_range.key
    #       }
    #     }
    #   }
    # }

  }
  # depends_on = [
  #   google_project_iam_member.shared_vpc,
  # ]
}

resource "google_storage_bucket_object" "load-templated-dag-files" {
  depends_on = [google_composer_environment.orch-cmp-0]
  for_each   = { for wf in local.workflows : wf.name => wf }
  name       = "dags/api_workflow_${each.key}.py"
  bucket     = replace(replace(google_composer_environment.orch-cmp-0.config[0].dag_gcs_prefix, "/dags", ""), "gs://", "")
  content = templatefile(
    "${path.root}/../airflow/dags_template/generic-api-service.py",
    { WORKFLOW_CONFIG = jsonencode(each.value), WORKFLOW_KEY = each.key }
  )
  content_type = "text/x-python"
}
