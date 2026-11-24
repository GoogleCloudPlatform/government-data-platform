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

"""Regression guards for CWE-306 across all Terraform Cloud Functions v2 and
their service account IAM bindings in 1-foundations/.
"""

import re
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
FOUNDATIONS_DIR = REPO_ROOT / "1-foundations"
TF_API_CONNECTOR = FOUNDATIONS_DIR / "02-api-connector.tf"
TF_TRANSFORMATION = FOUNDATIONS_DIR / "06-transformation.tf"

_PUBLIC_PRINCIPALS = ("allUsers", "allAuthenticatedUsers")
_FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS = (
    "roles/iam.serviceAccountUser",
    "roles/run.invoker",
    "roles/cloudfunctions.invoker",
    "roles/secretmanager.secretAccessor",
    "roles/bigquery.dataEditor",
    "roles/bigquery.dataOwner",
    "roles/bigquery.admin",
)
_CF_SA_REFS = (
    "google_service_account.load-0-api-fnc-runner-sa",
    "google_service_account.load-0-api-fnc-invoker-sa",
    "google_service_account.transf-0-api-fnc-runner-sa",
    "google_service_account.transf-0-api-fnc-invoker-sa",
)


def _strip_hcl_comments(text: str) -> str:
    """Strip #, //, and /* ... */ comments while preserving quoted strings."""
    out: List[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _extract_hcl_blocks(
    text: str, header_pattern: str
) -> List[Tuple[Tuple[str, ...], str]]:
    """Extract top-level HCL blocks matching header_pattern with brace depth."""
    cleaned = _strip_hcl_comments(text)
    blocks: List[Tuple[Tuple[str, ...], str]] = []
    for match in re.finditer(header_pattern, cleaned):
        start_brace = match.end() - 1
        depth = 0
        i = start_brace
        n = len(cleaned)
        in_string = False
        while i < n:
            ch = cleaned[i]
            if in_string:
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blocks.append((match.groups(), cleaned[start_brace : i + 1]))
                        break
            i += 1
    return blocks


def _extract_hcl_list(text: str, key_pattern: str) -> List[str]:
    """Extract quoted string items from an HCL list assigned to key_pattern."""
    match = re.search(rf"{key_pattern}\s*=\s*\[", text)
    if not match:
        return []
    i = match.end()
    n = len(text)
    depth = 1
    in_string = False
    buf: List[str] = []
    while i < n and depth > 0:
        ch = text[i]
        if in_string:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
                buf.append(ch)
            elif ch == "[":
                depth += 1
                buf.append(ch)
            elif ch == "]":
                depth -= 1
                if depth > 0:
                    buf.append(ch)
            else:
                buf.append(ch)
        i += 1
    return re.findall(r'"([^"]+)"', "".join(buf))


def _extract_hcl_subblock(text: str, key_pattern: str) -> Optional[str]:
    """Extract a balanced { ... } subblock assigned to key_pattern."""
    match = re.search(rf"{key_pattern}\s*=\s*\{{", text)
    if not match:
        return None
    start_brace = match.end() - 1
    depth = 0
    in_string = False
    i = start_brace
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start_brace : i + 1]
        i += 1
    return None


def _all_tf_files() -> Dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(FOUNDATIONS_DIR.glob("*.tf"))
    }


def _find_public_principal_violations(tf_files: Dict[Path, str]) -> List[str]:
    violations: List[str] = []
    for path, raw in tf_files.items():
        cleaned = _strip_hcl_comments(raw)
        for principal in _PUBLIC_PRINCIPALS:
            if f'"{principal}"' in cleaned:
                violations.append(
                    f"{path.name} grants public principal '{principal}'."
                )
    return violations


def _find_cloud_function_violations(
    tf_files: Dict[Path, str],
) -> Tuple[List[str], List[str]]:
    found_functions: List[str] = []
    violations: List[str] = []

    for path, raw in tf_files.items():
        modules = _extract_hcl_blocks(raw, r'module\s+"([^"]+)"\s*\{')
        for (mod_name,), body in modules:
            if "modules/cloud-function-v2" not in body:
                continue
            found_functions.append(f"{path.name}:{mod_name}")

            if re.search(
                r'ingress_settings\s*=\s*"(?:ALLOW_ALL|ALLOW_INTERNAL_AND_GCLB)"',
                body,
            ):
                violations.append(
                    f"{path.name}:{mod_name} allows external internet ingress."
                )
            if not re.search(
                r'ingress_settings\s*=\s*"ALLOW_INTERNAL_ONLY"', body
            ):
                violations.append(
                    f"{path.name}:{mod_name} must set ingress_settings = \"ALLOW_INTERNAL_ONLY\"."
                )

            iam_block = _extract_hcl_subblock(body, r"\biam\b")
            invoker_members = (
                _extract_hcl_list(iam_block, r'"roles/run\.invoker"')
                if iam_block
                else []
            )
            if not invoker_members:
                violations.append(
                    f"{path.name}:{mod_name} must declare non-empty function-scoped roles/run.invoker."
                )
            for member in invoker_members:
                if member in _PUBLIC_PRINCIPALS or not member.startswith(
                    "serviceAccount:"
                ):
                    violations.append(
                        f"{path.name}:{mod_name} grants run.invoker to non-SA member: {member}"
                    )

            for binding_key in (r"\biam_bindings\b", r"\biam_bindings_additive\b"):
                sub = _extract_hcl_subblock(body, binding_key)
                if not sub:
                    continue
                if (
                    '"roles/run.invoker"' in sub
                    or '"roles/cloudfunctions.invoker"' in sub
                ):
                    member_matches = re.findall(r'member\s*=\s*"([^"]+)"', sub)
                    member_matches.extend(_extract_hcl_list(sub, r"\bmembers\b"))
                    for member in member_matches:
                        if member in _PUBLIC_PRINCIPALS or not member.startswith(
                            "serviceAccount:"
                        ):
                            violations.append(
                                f"{path.name}:{mod_name} grants invoker via {binding_key} "
                                f"to non-SA member: {member}"
                            )

            runtime_sa_match = re.search(
                r"(?m)^\s*service_account\s*=\s*([^\s\n]+)",
                body,
            )
            if not runtime_sa_match:
                violations.append(
                    f"{path.name}:{mod_name} must explicitly set a dedicated least-privilege service_account."
                )
            else:
                runtime_sa = runtime_sa_match.group(1).strip('"')
                if not re.search(
                    r"(?m)^\s*service_account_create\s*=\s*false\b", body
                ):
                    violations.append(
                        f"{path.name}:{mod_name} must set service_account_create = false."
                    )

                # Runtime SA must not be granted roles/run.invoker on its own function.
                for member in invoker_members:
                    if runtime_sa in member:
                        violations.append(
                            f"{path.name}:{mod_name} reuses runtime service_account '{runtime_sa}' "
                            f"as roles/run.invoker."
                        )

                # Runtime SA must not reuse the Eventarc trigger invoker SA.
                trigger_sa = re.search(
                    r"trigger_config\s*=\s*\{[^}]*service_account_email\s*=\s*([^\s\n]+)",
                    body,
                    re.DOTALL,
                )
                if trigger_sa and trigger_sa.group(1).strip('"') == runtime_sa:
                    violations.append(
                        f"{path.name}:{mod_name} reuses trigger SA as runtime service_account."
                    )

        # Standalone raw Cloud Function resources must also enforce internal ingress + explicit SA.
        raw_cf_resources = _extract_hcl_blocks(
            raw,
            r'resource\s+"(google_cloudfunctions2?_function)"\s+"([^"]+)"\s*\{',
        )
        for (res_type, res_name), body in raw_cf_resources:
            found_functions.append(f"{path.name}:{res_type}.{res_name}")
            if not re.search(
                r'ingress_settings\s*=\s*"ALLOW_INTERNAL_ONLY"', body
            ):
                violations.append(
                    f"{path.name}:{res_type}.{res_name} must set ingress_settings = \"ALLOW_INTERNAL_ONLY\"."
                )
            if not re.search(r"\bservice_account_email\s*=", body):
                violations.append(
                    f"{path.name}:{res_type}.{res_name} must explicitly set service_account_email."
                )

        # Standalone Cloud Run / Cloud Functions IAM resources must never grant invoker to non-SA principals.
        iam_resources = _extract_hcl_blocks(
            raw,
            r'resource\s+"(google_(?:cloud_run_(?:v2_)?service|cloudfunctions2?_function)_iam_(?:member|binding|policy))"\s+"([^"]+)"\s*\{',
        )
        for (res_type, res_name), body in iam_resources:
            if (
                '"roles/run.invoker"' not in body
                and '"roles/cloudfunctions.invoker"' not in body
            ):
                continue
            members = re.findall(r'member\s*=\s*"([^"]+)"', body)
            members.extend(_extract_hcl_list(body, r"\bmembers\b"))
            if not members:
                violations.append(
                    f"{path.name}:{res_type}.{res_name} grants invoker without explicit serviceAccount members."
                )
            for member in members:
                if member in _PUBLIC_PRINCIPALS or not member.startswith(
                    "serviceAccount:"
                ):
                    violations.append(
                        f"{path.name}:{res_type}.{res_name} grants invoker to non-SA member: {member}"
                    )

    return found_functions, violations


def _find_forbidden_cf_sa_project_role_violations(
    tf_files: Dict[Path, str],
) -> List[str]:
    violations: List[str] = []

    for path, raw in tf_files.items():
        # 1. Direct project IAM resources.
        resources = _extract_hcl_blocks(
            raw,
            r'resource\s+"(google_project_iam_(?:member|binding|policy))"\s+"([^"]+)"\s*\{',
        )
        for (res_type, res_name), body in resources:
            if not any(sa_ref in body for sa_ref in _CF_SA_REFS):
                continue
            for role in _FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS:
                if f'"{role}"' in body:
                    violations.append(
                        f"{path.name} ({res_type}.{res_name}) grants overly broad "
                        f"project-wide '{role}' to a Cloud Function service account."
                    )

        # 2. Fabric project modules (iam, iam_by_principals, iam_bindings[_additive]).
        modules = _extract_hcl_blocks(raw, r'module\s+"([^"]+)"\s*\{')
        for (mod_name,), body in modules:
            if "modules/project" in body:
                iam_block = _extract_hcl_subblock(body, r"\biam\b")
                if iam_block:
                    for role in _FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS:
                        members = _extract_hcl_list(
                            iam_block, rf'"{re.escape(role)}"'
                        )
                        if any(
                            sa_ref in member
                            for member in members
                            for sa_ref in _CF_SA_REFS
                        ):
                            violations.append(
                                f"{path.name} (module.{mod_name}.iam) grants overly broad "
                                f"project-wide '{role}' to a Cloud Function service account."
                            )

                by_principals = _extract_hcl_subblock(
                    body, r"\biam_by_principals\b"
                )
                if by_principals:
                    for sa_ref in _CF_SA_REFS:
                        if sa_ref not in by_principals:
                            continue
                        roles = _extract_hcl_list(
                            by_principals, rf'"[^"]*{re.escape(sa_ref)}[^"]*"'
                        )
                        for role in _FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS:
                            if role in roles:
                                violations.append(
                                    f"{path.name} (module.{mod_name}.iam_by_principals) grants "
                                    f"project-wide '{role}' to '{sa_ref}'."
                                )

                for binding_key in (
                    r"\biam_bindings\b",
                    r"\biam_bindings_additive\b",
                ):
                    sub = _extract_hcl_subblock(body, binding_key)
                    if not sub or not any(
                        sa_ref in sub for sa_ref in _CF_SA_REFS
                    ):
                        continue
                    for role in _FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS:
                        if f'"{role}"' in sub:
                            violations.append(
                                f"{path.name} (module.{mod_name}.{binding_key}) grants "
                                f"project-wide '{role}' to a Cloud Function service account."
                            )

            # 3. Fabric iam-service-account modules targeting a CF SA with iam_project_roles.
            if "iam_project_roles" in body and any(
                sa_ref in body for sa_ref in _CF_SA_REFS
            ):
                proj_roles = _extract_hcl_subblock(body, r"\biam_project_roles\b")
                if proj_roles:
                    for role in _FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS:
                        if f'"{role}"' in proj_roles:
                            violations.append(
                                f"{path.name} (module.{mod_name}.iam_project_roles) grants "
                                f"project-wide '{role}' to a Cloud Function service account."
                            )

    return violations


class TestCloudFunctionsAuthCWE306(unittest.TestCase):
    def setUp(self):
        self.tf_files = _all_tf_files()

    def test_no_public_iam_principals_in_foundations(self):
        violations = _find_public_principal_violations(self.tf_files)
        self.assertEqual(violations, [], "\n".join(violations))

    def test_every_cloud_function_v2_enforces_internal_ingress_and_scoped_invoker(self):
        found_functions, violations = _find_cloud_function_violations(
            self.tf_files
        )
        self.assertEqual(violations, [], "\n".join(violations))
        self.assertIn("02-api-connector.tf:load-0-api-fnc", found_functions)
        self.assertIn("06-transformation.tf:transf-0-api-fnc", found_functions)

    def test_no_project_wide_act_as_or_invoker_roles_on_cf_service_accounts(self):
        violations = _find_forbidden_cf_sa_project_role_violations(self.tf_files)
        self.assertEqual(violations, [], "\n".join(violations))

    def test_load_api_connector_least_privilege_iam(self):
        cleaned = _strip_hcl_comments(TF_API_CONNECTOR.read_text(encoding="utf-8"))
        load_modules = dict(
            (name, body)
            for (name,), body in _extract_hcl_blocks(
                cleaned, r'module\s+"([^"]+)"\s*\{'
            )
        )
        self.assertIn("load-0-api-fnc", load_modules)
        fnc_body = load_modules["load-0-api-fnc"]

        # load-0-api-fnc must run as its dedicated runner SA (never load-0-api-sa or invoker SA).
        self.assertRegex(
            fnc_body,
            r"(?m)^\s*service_account\s*=\s*google_service_account\.load-0-api-fnc-runner-sa\.email\b",
        )
        self.assertRegex(
            fnc_body, r"(?m)^\s*service_account_create\s*=\s*false\b"
        )
        self.assertRegex(
            fnc_body, r'ingress_settings\s*=\s*"ALLOW_INTERNAL_ONLY"'
        )

        # Function-scoped roles/run.invoker must contain ONLY BQ connection SA + Cloud Tasks invoker SA.
        iam_block = _extract_hcl_subblock(fnc_body, r"\biam\b")
        self.assertIsNotNone(iam_block)
        assert iam_block is not None
        invokers = _extract_hcl_list(iam_block, r'"roles/run\.invoker"')
        self.assertEqual(
            set(invokers),
            {
                "serviceAccount:${google_bigquery_connection.load-connection-bq-0.cloud_resource[0].service_account_id}",
                "serviceAccount:${google_service_account.load-0-api-fnc-invoker-sa.email}",
            },
        )

        # Runner SA project-level IAM is restricted strictly to roles/cloudtasks.enqueuer.
        project_iam_blocks = dict(
            (res_name, body)
            for (_, res_name), body in _extract_hcl_blocks(
                cleaned,
                r'resource\s+"(google_project_iam_member)"\s+"([^"]+)"\s*\{',
            )
        )
        self.assertIn("load-0-api-fnc-runner-sa-iam", project_iam_blocks)
        runner_roles = re.findall(
            r'"(roles/[^"]+)"',
            project_iam_blocks["load-0-api-fnc-runner-sa-iam"],
        )
        self.assertEqual(runner_roles, ["roles/cloudtasks.enqueuer"])

        # Invoker SA must have zero project-wide IAM bindings in 02-api-connector.tf.
        for res_name, body in project_iam_blocks.items():
            self.assertNotIn(
                "google_service_account.load-0-api-fnc-invoker-sa",
                body,
                f"02-api-connector.tf ({res_name}) grants project-wide IAM to load-0-api-fnc-invoker-sa.",
            )

        # Runner SA BigQuery write access is scoped strictly to dwh-load-bq-0 dataset.
        bq_iam_blocks = _extract_hcl_blocks(
            cleaned,
            r'resource\s+"google_bigquery_dataset_iam_member"\s+"([^"]+)"\s*\{',
        )
        self.assertTrue(
            any(
                "module.dwh-load-bq-0.dataset_id" in body
                and '"roles/bigquery.dataEditor"' in body
                and "google_service_account.load-0-api-fnc-runner-sa.email"
                in body
                for _, body in bq_iam_blocks
            ),
            "load-0-api-fnc-runner-sa must have dataset-scoped roles/bigquery.dataEditor "
            "on module.dwh-load-bq-0.",
        )

        # Runner SA actAs is scoped to load-0-api-fnc-invoker-sa only.
        sa_iam_blocks = _extract_hcl_blocks(
            cleaned,
            r'resource\s+"google_service_account_iam_member"\s+"([^"]+)"\s*\{',
        )
        self.assertTrue(
            any(
                "google_service_account.load-0-api-fnc-invoker-sa.name" in body
                and '"roles/iam.serviceAccountUser"' in body
                and "google_service_account.load-0-api-fnc-runner-sa.email"
                in body
                for _, body in sa_iam_blocks
            ),
            "load-0-api-fnc-runner-sa must have SA-scoped roles/iam.serviceAccountUser "
            "on load-0-api-fnc-invoker-sa.",
        )

    def test_transformation_postprocess_least_privilege_iam(self):
        cleaned = _strip_hcl_comments(TF_TRANSFORMATION.read_text(encoding="utf-8"))

        # Dedicated least-privilege runtime SA for transf-0-api-fnc.
        self.assertRegex(
            cleaned,
            r'resource\s+"google_service_account"\s+"transf-0-api-fnc-runner-sa"\s*\{',
        )
        transf_modules = dict(
            (name, body)
            for (name,), body in _extract_hcl_blocks(
                cleaned, r'module\s+"([^"]+)"\s*\{'
            )
        )
        self.assertIn("transf-0-api-fnc", transf_modules)
        fnc_body = transf_modules["transf-0-api-fnc"]
        self.assertRegex(
            fnc_body,
            r"(?m)^\s*service_account\s*=\s*google_service_account\.transf-0-api-fnc-runner-sa\.email\b",
        )
        self.assertRegex(
            fnc_body, r"(?m)^\s*service_account_create\s*=\s*false\b"
        )
        iam_block = _extract_hcl_subblock(fnc_body, r"\biam\b")
        self.assertIsNotNone(iam_block)
        assert iam_block is not None
        self.assertEqual(
            _extract_hcl_list(iam_block, r'"roles/run\.invoker"'),
            [
                "serviceAccount:${google_service_account.transf-0-api-fnc-invoker-sa.email}"
            ],
        )

        # Eventarc custom trigger SA needs eventReceiver + Pub/Sub SA-scoped tokenCreator.
        self.assertIn('"roles/eventarc.eventReceiver"', cleaned)
        self.assertIn(
            "google_service_account.transf-0-api-fnc-invoker-sa.email", cleaned
        )
        sa_iam_blocks = _extract_hcl_blocks(
            cleaned,
            r'resource\s+"google_service_account_iam_member"\s+"([^"]+)"\s*\{',
        )
        self.assertTrue(
            any(
                "google_service_account.transf-0-api-fnc-invoker-sa.name" in body
                and '"roles/iam.serviceAccountTokenCreator"' in body
                and "module.transf-project.service_agents.pubsub.iam_email" in body
                for _, body in sa_iam_blocks
            ),
            "Pub/Sub service agent must have SA-scoped roles/iam.serviceAccountTokenCreator "
            "on transf-0-api-fnc-invoker-sa.",
        )

        # load-0-api-fnc-runner-sa gets topic-scoped pubsub.publisher on transf-ps-0.
        self.assertIn("transf-ps-0", transf_modules)
        ps_body = transf_modules["transf-ps-0"]
        self.assertIn('"roles/pubsub.publisher"', ps_body)
        self.assertIn(
            '"serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"',
            ps_body,
        )


class TestCloudFunctionsAuthSmallestFailingCases(unittest.TestCase):
    """Smallest-case reproductions ensuring the CWE-306 HCL guards catch every
    regression vector from commit 1bb6ab69 and IAM bypass variants.
    """

    def test_smallest_failing_case_allow_all_and_missing_ingress(self):
        allow_all_tf = {
            Path("02-api-connector.tf"): """
module "load-0-api-fnc" {
  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  ingress_settings = "ALLOW_ALL"
  iam = {
    "roles/run.invoker" = ["serviceAccount:${google_service_account.load-0-api-fnc-invoker-sa.email}"]
  }
  service_account        = google_service_account.load-0-api-fnc-runner-sa.email
  service_account_create = false
}
"""
        }
        _, violations = _find_cloud_function_violations(allow_all_tf)
        self.assertTrue(
            any("external internet ingress" in v for v in violations),
            violations,
        )

        missing_ingress_tf = {
            Path("02-api-connector.tf"): """
module "load-0-api-fnc" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  iam = {
    "roles/run.invoker" = ["serviceAccount:${google_service_account.load-0-api-fnc-invoker-sa.email}"]
  }
  service_account        = google_service_account.load-0-api-fnc-runner-sa.email
  service_account_create = false
}
"""
        }
        _, violations = _find_cloud_function_violations(missing_ingress_tf)
        self.assertTrue(
            any("ALLOW_INTERNAL_ONLY" in v for v in violations), violations
        )

    def test_smallest_failing_case_all_users_and_non_sa_invokers(self):
        all_users_tf = {
            Path("02-api-connector.tf"): """
module "load-0-api-fnc" {
  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  ingress_settings = "ALLOW_INTERNAL_ONLY"
  iam = {
    "roles/run.invoker" = ["allUsers"]
  }
  service_account        = google_service_account.load-0-api-fnc-runner-sa.email
  service_account_create = false
}
"""
        }
        self.assertTrue(_find_public_principal_violations(all_users_tf))
        _, violations = _find_cloud_function_violations(all_users_tf)
        self.assertTrue(any("non-SA member" in v for v in violations), violations)

        standalone_iam_tf = {
            Path("02-api-connector.tf"): """
resource "google_cloud_run_v2_service_iam_member" "public" {
  role   = "roles/run.invoker"
  member = "allAuthenticatedUsers"
}
"""
        }
        self.assertTrue(_find_public_principal_violations(standalone_iam_tf))
        _, violations = _find_cloud_function_violations(standalone_iam_tf)
        self.assertTrue(any("non-SA member" in v for v in violations), violations)

    def test_smallest_failing_case_missing_or_conflated_runtime_service_account(
        self,
    ):
        missing_sa_tf = {
            Path("02-api-connector.tf"): """
module "load-0-api-fnc" {
  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  ingress_settings = "ALLOW_INTERNAL_ONLY"
  iam = {
    "roles/run.invoker" = ["serviceAccount:${google_service_account.load-0-api-fnc-invoker-sa.email}"]
  }
}
"""
        }
        _, violations = _find_cloud_function_violations(missing_sa_tf)
        self.assertTrue(
            any("service_account" in v for v in violations), violations
        )

        self_invoking_tf = {
            Path("02-api-connector.tf"): """
module "load-0-api-fnc" {
  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  ingress_settings = "ALLOW_INTERNAL_ONLY"
  iam = {
    "roles/run.invoker" = ["serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"]
  }
  service_account        = google_service_account.load-0-api-fnc-runner-sa.email
  service_account_create = false
}
"""
        }
        _, violations = _find_cloud_function_violations(self_invoking_tf)
        self.assertTrue(
            any("reuses runtime service_account" in v for v in violations),
            violations,
        )

        trigger_reused_tf = {
            Path("06-transformation.tf"): """
module "transf-0-api-fnc" {
  source           = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-function-v2?ref=v36.0.1"
  ingress_settings = "ALLOW_INTERNAL_ONLY"
  iam = {
    "roles/run.invoker" = ["serviceAccount:${google_service_account.transf-0-api-fnc-invoker-sa.email}"]
  }
  trigger_config = {
    service_account_email = google_service_account.transf-0-api-fnc-invoker-sa.email
  }
  service_account        = google_service_account.transf-0-api-fnc-invoker-sa.email
  service_account_create = false
}
"""
        }
        _, violations = _find_cloud_function_violations(trigger_reused_tf)
        self.assertTrue(
            any("reuses trigger SA" in v for v in violations), violations
        )

    def test_smallest_failing_case_project_wide_privileged_roles_on_cf_sa(self):
        project_member_tf = {
            Path("02-api-connector.tf"): """
resource "google_project_iam_member" "load-0-api-fnc-runner-sa-iam" {
  for_each = toset([
    "roles/cloudtasks.enqueuer",
    "roles/secretmanager.secretAccessor",
    "roles/bigquery.dataEditor",
    "roles/iam.serviceAccountUser",
  ])
  project = module.load-project.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"
}
"""
        }
        violations = _find_forbidden_cf_sa_project_role_violations(
            project_member_tf
        )
        self.assertEqual(len(violations), 3, violations)

        fabric_project_iam_tf = {
            Path("03-load.tf"): """
module "load-project" {
  source = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"
  iam = {
    "roles/secretmanager.secretAccessor" = [
      "serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"
    ]
  }
}
"""
        }
        violations = _find_forbidden_cf_sa_project_role_violations(
            fabric_project_iam_tf
        )
        self.assertTrue(
            any("roles/secretmanager.secretAccessor" in v for v in violations),
            violations,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
