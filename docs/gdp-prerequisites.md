# Government Data Platform (GDP) pre-requisites

Before getting in action with deploying GDP in your environment, please, make sure to comply with the following prerequisites.

## 1. Create a folder under your organization

[Folders](https://cloud.google.com/resource-manager/docs/creating-managing-folders) are nodes in the Cloud Platform Resource Hierarchy. A folder can contain projects, other folders, or a combination of both. Organizations can use folders to group projects under the organization node in a hierarchy.

Because GDP deploys multiple projects (one for each area in the [architecture](gdp-architecture.md)), it uses folders to make it easy to govern and maintain the environment. So, before running the steps described under [1-foundations](../1-foundations/README.md), you should create a new folder.

To create a new folder using `gcloud`, from a logged terminal run the following command line.

```
gcloud resource-manager folders create \
   --display-name={YOUR-FOLDER-DISPLAY-NAME} \
   --organization={YOUR-ORGANIZATION-ID}
```

## 2. Billing account properly set up

For the Terraform scripts to properly run, you will need to have a [Billing Account](https://cloud.google.com/billing/docs/how-to/manage-billing-account) properly set up (meaning, it is associated with the new projects that will be automatically created).

* To create a new billing account, please, [follow the steps described in this tutorial](https://cloud.google.com/billing/docs/how-to/create-billing-account).
* To attach an existing billing account to a new organization and its projects, please, [follow the steps described here](https://cloud.google.com/billing/docs/how-to/modify-project).

## 3. Set up a Service Account for deployment

GDP can be deployed through a regular user account with the proper permissions, however, that's not recommended. Rather, you should look for an approach based on [Service Accounts (SA)](https://cloud.google.com/iam/docs/service-accounts), that can run on behalf of an application, process or cloud service.

For deploying GDP, you can either use an existing service account and set up the proper permissions (see [prerequisite 4](#4-set-up-permissions-for-the-service-account)) or [create](https://cloud.google.com/iam/docs/service-accounts-create) a new one and set up the proper IAM roles for it later.

For this guide, we assume you are running code in a single-user development environment and recommend [use service account impersonation](https://cloud.google.com/docs/authentication/use-service-account-impersonation). You [can choose the right authentication method for your use case](https://cloud.google.com/docs/authentication?#auth-decision-tree).

To set up the gcloud CLI to use the identity and access provided by a service account by default, you use the gcloud CLI config command:

```
gcloud config set auth/impersonate_service_account SERVICE_AC_CREATED_EMAIL
```

## 4. Set up permissions for the Service Account

The Government Data Platform is meant to be executed by a Service Account (or a regular user) having this minimal set of permission:

* Billing account:
  * `roles/billing.user`
* Folder level:
  * `roles/resourcemanager.folderAdmin`
  * `roles/resourcemanager.projectCreator`
* KMS Keys (If CMEK encryption in use):
  * `roles/cloudkms.admin` or a custom role with `cloudkms.cryptoKeys.getIamPolicy`, `cloudkms.cryptoKeys.list`, `cloudkms.cryptoKeys.setIamPolicy` permissions
* Shared VPC host project (if configured):
  * `roles/compute.xpnAdmin` on the host project folder or organization
  * `roles/resourcemanager.projectIamAdmin` on the host project, either with no conditions or with a condition allowing delegated role grants for `roles/compute.networkUser`, `roles/composer.sharedVpcAgent`, `roles/container.hostServiceAgentUser`

## 5. Organization Policy API (eventually needed)

Additionally, we recommend doing the following (only if necessary).

* Active the `Organization Policy API` at the organization level where GDP's projects will be deployed.
* To the Service Account, do attribute the roles of `Owner` and `Organization Policy Administrator` at the organization level.

## 6. User groups

User groups provide a stable frame of reference that allows decoupling the final set of permissions from the stage where entities and resources are created, and their IAM bindings defined.

We use three groups to control access to resources:

- *Data Engineers*. They handle and run the Data Hub, with read access to all resources in order to troubleshoot possible issues with pipelines. This team can also impersonate any service account.
- *Data Analysts*. They perform analysis on datasets, with read access to the Data Warehouse Confidential project, and BigQuery READ/WRITE access to the playground project.
- *Data Security*. They handle security configurations related to the Data Hub. This team has admin access to the common project to configure Cloud DLP templates or Data Catalog policy tags.

The table below shows a high-level overview of roles for each group on each project, using `READ`, `WRITE` and `ADMIN` access patterns for simplicity. For detailed roles please refer to the code.

|Group|Drop off|Load|Transformation|DHW Landing|DWH Curated|DWH Confidential|DWH Playground|Orchestration|Common|
|-|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
|Data Engineers|`ADMIN`|`ADMIN`|`ADMIN`|`ADMIN`|`ADMIN`|`ADMIN`|`ADMIN`|`ADMIN`|`ADMIN`|
|Data Analysts|-|-|-|-|-|`READ`|`READ`/`WRITE`|-|-|
|Data Security|-|-|-|-|-|-|-|-|`ADMIN`|

Before deploying GDP you need to certify that the three groups mentioned above exist in Google Cloud Identity and Access Management (IAM).

You can  opt for using the default group names defined in the GDP foundations scripts. In this case, you should create in advance the following three groups in your organization:

- gcp-data-engineers
- gcp-data-analysts
- gcp-data-security

You can find more information about creating groups in IAM [here](https://cloud.google.com/iam/docs/groups-in-cloud-console#creating).

Another option is using your organization pre-defined groups for these three roles. In order to achieve this, you can configure groups by setting the `groups` variable in the `terraform.tfvars` file. Further information on GDP foundation deployment and customization is provided [here](../1-foundations/README.md).


## 7. Virtual Private Cloud (VPC) design

As is often the case in real-world configurations, this blueprint accepts as input an existing [Shared-VPC](https://cloud.google.com/vpc/docs/shared-vpc) via the `network_config` variable. Make sure that the GKE API (`container.googleapis.com`) is enabled in the VPC host project.

If the `network_config` variable is not provided, one VPC will be created in each project that supports network resources (load, transformation, and orchestration projects).

### IP ranges and subnetting

To deploy this blueprint with self-managed VPCs you need the following ranges:

- one /24 for the load project VPC subnet used for Cloud Dataflow workers
- one /24 for the transformation VPC subnet used for Cloud Dataflow workers
- one /24 range for the orchestration VPC subnet used for Composer workers
- one /22 and one /24 ranges for the secondary ranges associated with the orchestration VPC subnet

If you are using Shared VPC, you need one subnet with one /22 and one /24 secondary range defined for Composer pods and services.

In both VPC scenarios, you also need these ranges for Composer:

- one /24 for Cloud SQL
- one /28 for the GKE control plane
- one /28 for the webserver