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

"""Regression guards for arbitrary BigQuery table write (CWE-639 / CWE-284)
via unvalidated result_table and project-wide roles/bigquery.dataEditor, plus
defense-in-depth checks for CLIENT_CREDENTIALS auth_server exfiltration.
"""

import json
import os
import re
import socket
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
API_CONNECTOR_DIR = REPO_ROOT / "api-connector"
TF_API_CONNECTOR = REPO_ROOT / "1-foundations" / "02-api-connector.tf"

os.environ.setdefault("PROJECT_ID", "gdp-test-lod")
os.environ.setdefault("PROJECT_NUMBER", "123456789012")
os.environ.setdefault("REGION", "us-west1")
os.environ.setdefault("FUNCTION_NAME", "load-0-api-fnc")
os.environ.setdefault(
    "TASK_INVOKER_SA",
    "api-src-fnc-sa-invoker@gdp-test-lod.iam.gserviceaccount.com",
)
os.environ.setdefault("ALLOWED_QUEUE_PREFIX", "cloud-task-api-")
os.environ.setdefault("ALLOWED_BQ_DATASET", "gdp_test_dwh_load_bq_0")


def _install_optional_stubs() -> None:
    """Install minimal stubs when third-party Cloud Function deps are absent."""
    if "coloredlogs" not in sys.modules:
        try:
            import coloredlogs  # noqa: F401
        except ImportError:
            mod = types.ModuleType("coloredlogs")
            mod.install = lambda *args, **kwargs: None
            sys.modules["coloredlogs"] = mod

    if "flask" not in sys.modules:
        try:
            import flask  # noqa: F401
        except ImportError:
            mod = types.ModuleType("flask")

            class _Request:
                pass

            mod.Request = _Request
            sys.modules["flask"] = mod

    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ImportError:
            req_mod = types.ModuleType("requests")
            auth_mod = types.ModuleType("requests.auth")

            class AuthBase:
                pass

            def _unmocked(*args, **kwargs):
                raise RuntimeError("requests call must be mocked in unit tests.")

            auth_mod.AuthBase = AuthBase
            req_mod.auth = auth_mod
            req_mod.get = _unmocked
            req_mod.post = _unmocked
            req_mod.put = _unmocked
            req_mod.delete = _unmocked
            sys.modules["requests"] = req_mod
            sys.modules["requests.auth"] = auth_mod

    for pkg in ("google", "google.cloud"):
        if pkg not in sys.modules:
            try:
                __import__(pkg)
            except ImportError:
                mod = types.ModuleType(pkg)
                mod.__path__ = []
                sys.modules[pkg] = mod

    try:
        from google.cloud import tasks_v2  # noqa: F401
    except ImportError:
        tasks_mod = types.ModuleType("google.cloud.tasks_v2")

        class HttpMethod:
            POST = 1

        @dataclass
        class OidcToken:
            service_account_email: str = ""
            audience: str = ""

        @dataclass
        class HttpRequest:
            http_method: Any = None
            url: str = ""
            headers: Any = None
            body: Optional[bytes] = None
            oidc_token: Optional[OidcToken] = None

        @dataclass
        class Task:
            http_request: Optional[HttpRequest] = None

        class CloudTasksClient:
            def create_task(self, request=None):
                raise RuntimeError("CloudTasksClient.create_task must be mocked.")

        tasks_mod.HttpMethod = HttpMethod
        tasks_mod.OidcToken = OidcToken
        tasks_mod.HttpRequest = HttpRequest
        tasks_mod.Task = Task
        tasks_mod.CloudTasksClient = CloudTasksClient
        sys.modules["google.cloud.tasks_v2"] = tasks_mod
        sys.modules["google.cloud"].tasks_v2 = tasks_mod

    for sub in ("pubsub_v1", "secretmanager", "bigquery"):
        full = f"google.cloud.{sub}"
        try:
            mod = __import__(full, fromlist=[sub])
        except ImportError:
            mod = types.ModuleType(full)
            sys.modules[full] = mod
            setattr(sys.modules["google.cloud"], sub, mod)
        if sub == "bigquery" and not hasattr(mod, "Client"):

            class Client:
                def __init__(self, project=None):
                    raise RuntimeError("bigquery.Client must be mocked in unit tests.")

            mod.Client = Client


_install_optional_stubs()
if str(API_CONNECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(API_CONNECTOR_DIR))

from src.config import config  # noqa: E402
from src.handler import Handler  # noqa: E402
from src.utils import Utils  # noqa: E402
from src.workflow_policy import (  # noqa: E402
    WorkflowPolicyConfigError,
    resolve_result_and_log_tables,
    validate_result_table,
)

PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

ALLOWED_WORKFLOWS_FIXTURE = json.dumps(
    {
        "workflow1": {
            "uri": "https://api.example.com/v1/data",
            "method": "GET",
            "auth_type": "NONE",
            "secret_name": None,
            "auth_server": None,
            "client_id": None,
        },
        "oauth_workflow": {
            "uri": "https://api.partner.example/v1/records",
            "method": "POST",
            "auth_type": "CLIENT_CREDENTIALS",
            "secret_name": "projects/123456789012/secrets/oauth-client/versions/1",
            "auth_server": "https://auth.partner.example/oauth2/token",
            "client_id": "gdp-client-id",
        },
    }
)


class _FakeFlaskRequest:
    def __init__(self, payload: Any):
        self._payload = payload

    def get_json(self, silent: bool = False) -> Any:
        return self._payload


def _make_mock_http_response(status_code: int = 200, body: str = '{"ok": true}'):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Content-Type": "application/json"}
    resp.text = body
    resp.json.return_value = json.loads(body)
    resp.elapsed.total_seconds.return_value = 0.05
    return resp


class TestResultTableAllowlistValidation(unittest.TestCase):
    def setUp(self):
        self._patch = patch.object(
            config, "ALLOWED_BQ_DATASET", "gdp_test_dwh_load_bq_0"
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_accepts_allowed_dataset_and_canonical_run_tables(self):
        cases = [
            (
                "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
                "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            ),
            (
                "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            ),
            (
                "gdp_test_dwh_load_bq_0.tbl_result_550e8400-e29b-41d4-a716-446655440000",
                "gdp_test_dwh_load_bq_0.tbl_result_550e8400-e29b-41d4-a716-446655440000",
                "gdp_test_dwh_load_bq_0.tbl_process_log_550e8400-e29b-41d4-a716-446655440000",
            ),
            # Run ID containing prefix substrings must not corrupt table mapping.
            (
                "gdp_test_dwh_load_bq_0.tbl_process_log_2025_tbl_result_1",
                "gdp_test_dwh_load_bq_0.tbl_result_2025_tbl_result_1",
                "gdp_test_dwh_load_bq_0.tbl_process_log_2025_tbl_result_1",
            ),
        ]
        for raw, expected_result, expected_log in cases:
            with self.subTest(raw=raw):
                self.assertEqual(validate_result_table(raw), raw)
                self.assertEqual(
                    resolve_result_and_log_tables(raw),
                    (expected_result, expected_log),
                )

    def test_rejects_arbitrary_datasets_tables_decorators_and_malformed_inputs(self):
        disallowed_tables = [
            # Arbitrary dataset in same load project
            "other_sensitive_dataset.arbitrary_victim_table",
            "other_sensitive_dataset.tbl_result_20250101000000",
            "other_sensitive_dataset.tbl_process_log_20250101000000",
            # Dataset prefix spoof & case mismatch
            "gdp_test_dwh_load_bq_0_evil.tbl_result_20250101000000",
            "GDP_TEST_DWH_LOAD_BQ_0.tbl_result_20250101000000",
            # Arbitrary table inside the allowed dataset
            "gdp_test_dwh_load_bq_0.arbitrary_victim_table",
            "gdp_test_dwh_load_bq_0.lod_ingestion_data_20250101000000",
            "gdp_test_dwh_load_bq_0.tbl_other_20250101000000",
            "gdp_test_dwh_load_bq_0.tbl_result",
            "gdp_test_dwh_load_bq_0.tbl_result_",
            "gdp_test_dwh_load_bq_0.tbl_process_log_",
            "gdp_test_dwh_load_bq_0.tbl_result_-leadinghyphen",
            # Cross-project 3-part reference
            "victim-proj.gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            # BigQuery partition / snapshot decorators
            "gdp_test_dwh_load_bq_0.tbl_result_20250101$20250101",
            "gdp_test_dwh_load_bq_0.tbl_result_20250101@12345",
            # Injection, traversal, backticks, whitespace, newline
            "gdp_test_dwh_load_bq_0.tbl_result_20250101\n",
            " gdp_test_dwh_load_bq_0.tbl_result_20250101",
            "`gdp_test_dwh_load_bq_0.tbl_result_20250101`",
            "gdp_test_dwh_load_bq_0.tbl_result_../other",
            "gdp_test_dwh_load_bq_0.tbl_result_" + "a" * 101,
            # Missing / non-string
            "",
            None,
            12345,
            ["gdp_test_dwh_load_bq_0.tbl_result_20250101000000"],
        ]
        for bad_table in disallowed_tables:
            with self.subTest(result_table=bad_table):
                with self.assertRaises(ValueError):
                    validate_result_table(bad_table)
                with self.assertRaises(ValueError):
                    resolve_result_and_log_tables(bad_table)

    def test_fails_closed_when_allowed_bq_dataset_missing_or_malformed(self):
        valid_table = "gdp_test_dwh_load_bq_0.tbl_result_20250101000000"
        for bad_dataset in (None, "", "bad.dataset", "proj:dataset", "../evil"):
            with self.subTest(allowed_dataset=bad_dataset):
                with patch.object(config, "ALLOWED_BQ_DATASET", bad_dataset):
                    with self.assertRaises(WorkflowPolicyConfigError):
                        validate_result_table(valid_table)


class TestCloudTaskBigQueryTableWriteSecurity(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(config, "PROJECT_ID", "gdp-test-lod"),
            patch.object(config, "PROJECT_NUMBER", "123456789012"),
            patch.object(config, "REGION", "us-west1"),
            patch.object(config, "FUNCTION_NAME", "load-0-api-fnc"),
            patch.object(config, "FUNCTION_URI", None),
            patch.object(
                config,
                "TASK_INVOKER_SA",
                "api-src-fnc-sa-invoker@gdp-test-lod.iam.gserviceaccount.com",
            ),
            patch.object(config, "ALLOWED_QUEUE_PREFIX", "cloud-task-api-"),
            patch.object(config, "ALLOWED_BQ_DATASET", "gdp_test_dwh_load_bq_0"),
            patch.object(config, "ALLOWED_WORKFLOWS", ALLOWED_WORKFLOWS_FIXTURE),
            patch.object(config, "PUBSUB_TOPICS", None),
            patch(
                "src.workflow_policy.socket.getaddrinfo", return_value=PUBLIC_ADDRINFO
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @patch("src.utils.bigquery.Client")
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_blocks_arbitrary_dataset_and_table_write_reproduction(
        self, mock_get_secret, mock_requests_get, mock_bq_client_cls
    ):
        """Smallest failing reproduction: CLOUD_TASK targeting an arbitrary
        dataset or non-run table in the load project must fail before HTTP,
        Secret Manager, or BigQuery are touched.
        """
        attack_tables = [
            "other_sensitive_dataset.arbitrary_victim_table",
            "other_sensitive_dataset.tbl_result_20250101000000",
            "gdp_test_dwh_load_bq_0.arbitrary_victim_table",
            "gdp_test_dwh_load_bq_0.lod_ingestion_data_20250101000000",
        ]
        for table in attack_tables:
            with self.subTest(result_table=table):
                req = _FakeFlaskRequest(
                    {
                        "source": "CLOUD_TASK",
                        "workflow_id": "workflow1",
                        "request_config": {
                            "uri": "https://api.example.com/v1/data",
                            "method": "GET",
                        },
                        "result_table": table,
                    }
                )
                body, status = Handler.execute(req)
                self.assertEqual(status, 400)
                self.assertEqual(body, "Invalid result_table parameter.")

        mock_get_secret.assert_not_called()
        mock_requests_get.assert_not_called()
        mock_bq_client_cls.assert_not_called()

    @patch("src.utils.bigquery.Client")
    @patch("src.handlers.cloud_task.requests.get")
    def test_fails_closed_with_503_when_allowed_bq_dataset_unconfigured(
        self, mock_requests_get, mock_bq_client_cls
    ):
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "request_config": {
                    "uri": "https://api.example.com/v1/data",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        with patch.object(config, "ALLOWED_BQ_DATASET", None):
            body, status = Handler.execute(req)

        self.assertEqual(status, 503)
        self.assertEqual(body, "Server configuration error for BigQuery destination.")
        mock_requests_get.assert_not_called()
        mock_bq_client_cls.assert_not_called()

    @patch("src.utils.bigquery.Client")
    @patch("src.handlers.cloud_task.requests.get")
    def test_authorized_cloud_task_writes_canonical_result_and_log_tables(
        self, mock_requests_get, mock_bq_client_cls
    ):
        mock_bq_client = MagicMock()
        mock_bq_client.insert_rows_json.return_value = []
        mock_bq_client_cls.return_value = mock_bq_client
        mock_requests_get.return_value = _make_mock_http_response(200, '{"ok": true}')

        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "request_config": {
                    "uri": "https://api.example.com/v1/data",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"status": "processed", "status_code": 200})
        self.assertEqual(
            [call.args[0] for call in mock_bq_client.get_table.call_args_list],
            [
                "gdp-test-lod.gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                "gdp-test-lod.gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            ],
        )


class TestBigQueryRoutineTableAndAuthServerSecurity(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(config, "PROJECT_ID", "gdp-test-lod"),
            patch.object(config, "PROJECT_NUMBER", "123456789012"),
            patch.object(config, "REGION", "us-west1"),
            patch.object(config, "FUNCTION_NAME", "load-0-api-fnc"),
            patch.object(config, "FUNCTION_URI", None),
            patch.object(
                config,
                "TASK_INVOKER_SA",
                "api-src-fnc-sa-invoker@gdp-test-lod.iam.gserviceaccount.com",
            ),
            patch.object(config, "ALLOWED_QUEUE_PREFIX", "cloud-task-api-"),
            patch.object(config, "ALLOWED_BQ_DATASET", "gdp_test_dwh_load_bq_0"),
            patch.object(config, "ALLOWED_WORKFLOWS", ALLOWED_WORKFLOWS_FIXTURE),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @patch("src.utils.bigquery.Client")
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_bq_routine_blocks_arbitrary_dataset_or_table_before_enqueue_or_bq_write(
        self, mock_tasks_cls, mock_bq_client_cls
    ):
        mock_tasks = MagicMock()
        mock_tasks_cls.return_value = mock_tasks
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101"
        )
        req = _FakeFlaskRequest(
            {
                "calls": [
                    [
                        "workflow1",
                        json.dumps(
                            {"uri": "https://api.example.com/v1/data", "method": "GET"}
                        ),
                        json.dumps({"type": "NONE"}),
                        "{}",
                        "{}",
                        "{}",
                        "other_sensitive_dataset.arbitrary_victim_table",
                        valid_queue,
                    ],
                    [
                        "workflow1",
                        json.dumps(
                            {"uri": "https://api.example.com/v1/data", "method": "GET"}
                        ),
                        json.dumps({"type": "NONE"}),
                        "{}",
                        "{}",
                        "{}",
                        "gdp_test_dwh_load_bq_0.lod_ingestion_data_20250101",
                        valid_queue,
                    ],
                ]
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["replies"],
            [
                {"error": "Invalid result_table parameter."},
                {"error": "Invalid result_table parameter."},
            ],
        )
        mock_tasks.create_task.assert_not_called()
        mock_bq_client_cls.assert_not_called()

    @patch("src.utils.bigquery.Client")
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_bq_routine_fails_closed_when_allowed_bq_dataset_missing(
        self, mock_tasks_cls, mock_bq_client_cls
    ):
        mock_tasks = MagicMock()
        mock_tasks_cls.return_value = mock_tasks
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101"
        )
        row = [
            "workflow1",
            json.dumps({"uri": "https://api.example.com/v1/data", "method": "GET"}),
            json.dumps({"type": "NONE"}),
            "{}",
            "{}",
            "{}",
            "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            valid_queue,
        ]
        with patch.object(config, "ALLOWED_BQ_DATASET", None):
            body, status = Handler.execute(_FakeFlaskRequest({"calls": [row]}))

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["replies"],
            [{"error": "Server configuration error for BigQuery destination."}],
        )
        mock_tasks.create_task.assert_not_called()
        mock_bq_client_cls.assert_not_called()

    @patch("src.utils.bigquery.Client")
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_bq_routine_blocks_client_credentials_attacker_auth_server_before_enqueue(
        self, mock_tasks_cls, mock_bq_client_cls
    ):
        """Defense-in-depth: BIGQUERY_ROUTINE must reject CLIENT_CREDENTIALS
        calls attempting to divert client_secret to an attacker auth_server.
        """
        mock_tasks = MagicMock()
        mock_tasks_cls.return_value = mock_tasks
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101"
        )
        valid_table = "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000"
        req = _FakeFlaskRequest(
            {
                "calls": [
                    [
                        "oauth_workflow",
                        json.dumps(
                            {
                                "uri": "https://api.partner.example/v1/records",
                                "method": "POST",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "CLIENT_CREDENTIALS",
                                "auth_server": "https://attacker.example/steal-secret",
                                "client_id": "gdp-client-id",
                                "secret_name": "projects/123456789012/secrets/oauth-client/versions/1",
                            }
                        ),
                        "{}",
                        "{}",
                        "{}",
                        valid_table,
                        valid_queue,
                    ]
                ]
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["replies"], [{"error": "Unauthorized workflow request."}]
        )
        mock_tasks.create_task.assert_not_called()
        mock_bq_client_cls.assert_not_called()


class TestSaveBigQuerySinkDefenseInDepth(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(config, "PROJECT_ID", "gdp-test-lod"),
            patch.object(config, "ALLOWED_BQ_DATASET", "gdp_test_dwh_load_bq_0"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @patch("src.utils.bigquery.Client")
    def test_direct_sink_blocks_arbitrary_dataset_table_and_cross_project_writes(
        self, mock_bq_client_cls
    ):
        with self.assertRaises(ValueError):
            Utils.save_bigquery(
                "other_sensitive_dataset.arbitrary_victim_table", {"a": 1}
            )
        with self.assertRaises(ValueError):
            Utils.save_bigquery(
                "gdp_test_dwh_load_bq_0.arbitrary_victim_table", {"a": 1}
            )
        with self.assertRaises(ValueError):
            Utils.save_bigquery(
                "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                {"a": 1},
                project_id="other-project",
            )
        with patch.object(config, "ALLOWED_BQ_DATASET", None):
            with self.assertRaises(WorkflowPolicyConfigError):
                Utils.save_bigquery(
                    "gdp_test_dwh_load_bq_0.tbl_result_20250101000000", {"a": 1}
                )

        mock_bq_client_cls.assert_not_called()

    @patch("src.utils.bigquery.Client")
    def test_direct_sink_allows_canonical_table_in_configured_project_and_dataset(
        self, mock_bq_client_cls
    ):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []
        mock_bq_client_cls.return_value = mock_client

        ok = Utils.save_bigquery(
            "gdp_test_dwh_load_bq_0.tbl_result_20250101000000", {"k": "v"}
        )
        self.assertTrue(ok)
        mock_bq_client_cls.assert_called_once_with(project="gdp-test-lod")
        mock_client.get_table.assert_called_once_with(
            "gdp-test-lod.gdp_test_dwh_load_bq_0.tbl_result_20250101000000"
        )
        mock_client.insert_rows_json.assert_called_once()


def _extract_hcl_block(text: str, header_pattern: str) -> Optional[str]:
    match = re.search(header_pattern, text)
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
                    return text[match.start() : i + 1]
        i += 1
    return None


class TestTerraformBigQueryLeastPrivilegeHardening(unittest.TestCase):
    def setUp(self):
        self.tf_text = TF_API_CONNECTOR.read_text(encoding="utf-8")

    def test_runner_sa_uses_dataset_scoped_bigquery_data_editor_not_project_wide(self):
        runner_block = _extract_hcl_block(
            self.tf_text,
            r'resource "google_project_iam_member" "load-0-api-fnc-runner-sa-iam"\s*\{',
        )
        self.assertIsNotNone(runner_block)
        assert runner_block is not None
        self.assertNotIn('"roles/bigquery.dataEditor"', runner_block)
        self.assertNotIn('"roles/bigquery.dataOwner"', runner_block)
        self.assertNotIn('"roles/bigquery.admin"', runner_block)

        dataset_iam_block = _extract_hcl_block(
            self.tf_text,
            r'resource "google_bigquery_dataset_iam_member" "load-0-api-fnc-runner-bq-dataset-editor"\s*\{',
        )
        self.assertIsNotNone(dataset_iam_block)
        assert dataset_iam_block is not None
        self.assertIn("project    = module.load-project.project_id", dataset_iam_block)
        self.assertIn("dataset_id = module.dwh-load-bq-0.dataset_id", dataset_iam_block)
        self.assertIn('role       = "roles/bigquery.dataEditor"', dataset_iam_block)
        self.assertIn(
            '"serviceAccount:${google_service_account.load-0-api-fnc-runner-sa.email}"',
            dataset_iam_block,
        )

    def test_load_function_configures_allowed_bq_dataset_and_remains_private(self):
        self.assertIn(
            "ALLOWED_BQ_DATASET   = module.dwh-load-bq-0.dataset_id", self.tf_text
        )
        self.assertIn('ingress_settings = "ALLOW_INTERNAL_ONLY"', self.tf_text)
        self.assertNotIn('"ALLOW_ALL"', self.tf_text)
        self.assertNotIn('"allUsers"', self.tf_text)
        self.assertNotIn('"allAuthenticatedUsers"', self.tf_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
