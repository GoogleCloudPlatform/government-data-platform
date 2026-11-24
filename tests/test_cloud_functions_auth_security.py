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
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
FOUNDATIONS_DIR = REPO_ROOT / "1-foundations"
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


def _all_tf_files() -> Dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(FOUNDATIONS_DIR.glob("*.tf"))
    }


class TestCloudFunctionsAuthCWE306(unittest.TestCase):
    def setUp(self):
        self.tf_files = _all_tf_files()

    def test_no_public_iam_principals_in_foundations(self):
        for path, raw in self.tf_files.items():
            cleaned = _strip_hcl_comments(raw)
            for principal in _PUBLIC_PRINCIPALS:
                with self.subTest(file=path.name, principal=principal):
                    self.assertNotIn(
                        f'"{principal}"',
                        cleaned,
                        f"{path.name} grants public principal '{principal}'.",
                    )

    def test_every_cloud_function_v2_enforces_internal_ingress_and_scoped_invoker(self):
        found_functions: List[str] = []

        for path, raw in self.tf_files.items():
            modules = _extract_hcl_blocks(raw, r'module\s+"([^"]+)"\s*\{')
            for (mod_name,), body in modules:
                if "modules/cloud-function-v2" not in body:
                    continue
                found_functions.append(f"{path.name}:{mod_name}")

                with self.subTest(module=f"{path.name}:{mod_name}"):
                    self.assertNotRegex(
                        body,
                        r'ingress_settings\s*=\s*"ALLOW_ALL"',
                        f"Module '{mod_name}' allows public internet ingress.",
                    )
                    self.assertRegex(
                        body,
                        r'ingress_settings\s*=\s*"ALLOW_INTERNAL_ONLY"',
                        f"Module '{mod_name}' must set ingress_settings = \"ALLOW_INTERNAL_ONLY\".",
                    )

                    invoker_members = _extract_hcl_list(body, r'"roles/run\.invoker"')
                    self.assertTrue(
                        invoker_members,
                        f"Module '{mod_name}' must declare non-empty function-scoped roles/run.invoker.",
                    )
                    for member in invoker_members:
                        self.assertNotIn(member, _PUBLIC_PRINCIPALS)
                        self.assertTrue(
                            member.startswith("serviceAccount:"),
                            f"Module '{mod_name}' grants run.invoker to non-SA member: {member}",
                        )

                    # Runtime SA must not reuse the Eventarc trigger invoker SA.
                    trigger_sa = re.search(
                        r"trigger_config\s*=\s*\{[^}]*service_account_email\s*=\s*([^\s\n]+)",
                        body,
                        re.DOTALL,
                    )
                    runtime_sa = re.search(
                        r"(?m)^\s*service_account\s*=\s*([^\s\n]+)",
                        body,
                    )
                    if trigger_sa and runtime_sa:
                        self.assertNotEqual(
                            trigger_sa.group(1),
                            runtime_sa.group(1),
                            f"Module '{mod_name}' reuses trigger SA as runtime service_account.",
                        )

        self.assertIn("02-api-connector.tf:load-0-api-fnc", found_functions)
        self.assertIn("06-transformation.tf:transf-0-api-fnc", found_functions)

    def test_no_project_wide_act_as_or_invoker_roles_on_cf_service_accounts(self):
        for path, raw in self.tf_files.items():
            resources = _extract_hcl_blocks(
                raw,
                r'resource\s+"(google_project_iam_(?:member|binding))"\s+"([^"]+)"\s*\{',
            )
            for (res_type, res_name), body in resources:
                targets_cf_sa = any(sa_ref in body for sa_ref in _CF_SA_REFS)
                if not targets_cf_sa:
                    continue
                for role in _FORBIDDEN_PROJECT_ROLES_FOR_CF_SAS:
                    with self.subTest(
                        file=path.name, resource=f"{res_type}.{res_name}", role=role
                    ):
                        self.assertNotIn(
                            f'"{role}"',
                            body,
                            f"{path.name} ({res_type}.{res_name}) grants overly broad "
                            f"project-wide '{role}' to a Cloud Function service account.",
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
            r"service_account\s*=\s*google_service_account\.transf-0-api-fnc-runner-sa\.email",
        )
        self.assertIn(
            '"serviceAccount:${google_service_account.transf-0-api-fnc-invoker-sa.email}"',
            fnc_body,
        )

        # Eventarc custom trigger SA needs eventReceiver + Pub/Sub SA-scoped tokenCreator.
        self.assertIn('"roles/eventarc.eventReceiver"', cleaned)
        self.assertIn("google_service_account.transf-0-api-fnc-invoker-sa.email", cleaned)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
