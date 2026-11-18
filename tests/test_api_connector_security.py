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

"""Regression guards for api-connector Cloud Tasks queue IDOR (CWE-639),
mandatory OIDC authentication on enqueued tasks, and Terraform ingress/IAM
hardening on load-0-api-fnc.
"""

import json
import os
import re
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


def _install_optional_stubs() -> None:
    """Install minimal stubs only when third-party Cloud Function deps are absent."""
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
            __import__(full)
        except ImportError:
            mod = types.ModuleType(full)
            sys.modules[full] = mod
            setattr(sys.modules["google.cloud"], sub, mod)


_install_optional_stubs()
if str(API_CONNECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(API_CONNECTOR_DIR))

from src.config import config  # noqa: E402
from src.handler import Handler  # noqa: E402
from src.handlers.bigquery import BigQueryRoutineRequest  # noqa: E402


class _FakeFlaskRequest:
    def __init__(self, payload: Any):
        self._payload = payload

    def get_json(self, silent: bool = False) -> Any:
        return self._payload


def _make_call_row(
    queue_name: Any,
    result_table: Any = "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
    workflow_id: str = "workflow1",
) -> list:
    return [
        workflow_id,
        json.dumps({"uri": "https://example.com/api", "method": "GET"}),
        json.dumps(
            {
                "type": "HTTP_BASIC",
                "secret_name": "projects/123456789012/secrets/api-secret/versions/latest",
            }
        ),
        json.dumps({"Content-Type": "application/json"}),
        json.dumps({"q1": "val1"}),
        json.dumps({"k1": "val1"}),
        result_table,
        queue_name,
    ]


class TestQueueNameValidation(unittest.TestCase):
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
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_accepts_valid_dag_timestamp_and_uuid_queues(self):
        valid_queues = [
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000",
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-550e8400-e29b-41d4-a716-446655440000",
        ]
        for queue in valid_queues:
            with self.subTest(queue=queue):
                self.assertEqual(
                    BigQueryRoutineRequest._validate_queue_name(queue), queue
                )

    def test_rejects_idor_traversal_and_malformed_queues(self):
        malicious_queues = [
            # Cross-project IDOR
            "projects/victim-project/locations/us-west1/queues/cloud-task-api-20250101000000",
            # Partial project-ID prefix spoof
            "projects/gdp-test-lod-evil/locations/us-west1/queues/cloud-task-api-20250101000000",
            # Cross-region IDOR
            "projects/gdp-test-lod/locations/us-central1/queues/cloud-task-api-20250101000000",
            # Arbitrary queue in same project without allowed prefix
            "projects/gdp-test-lod/locations/us-west1/queues/billing-queue",
            # Prefix with empty suffix
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-",
            # Path traversal
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000/../../other",
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-../other",
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-%2e%2e",
            # Trailing newline bypass (regex $ pitfall)
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000\n",
            # Extra segments / query string / whitespace
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000/tasks/1",
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000?a=b",
            " projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000",
            # Exceeds Cloud Tasks 100-char queue ID limit
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-" + "a" * 90,
            # Empty / non-string
            "",
            None,
            12345,
            ["projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"],
        ]
        for queue in malicious_queues:
            with self.subTest(queue=queue):
                with self.assertRaises(ValueError):
                    BigQueryRoutineRequest._validate_queue_name(queue)

    def test_fails_closed_when_server_config_incomplete(self):
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        with patch.object(config, "REGION", None):
            with self.assertRaises(ValueError):
                BigQueryRoutineRequest._validate_queue_name(valid_queue)

        with patch.object(config, "ALLOWED_QUEUE_PREFIX", "../bad"):
            with self.assertRaises(ValueError):
                BigQueryRoutineRequest._validate_queue_name(valid_queue)


class TestBigQueryRoutineDispatchSecurity(unittest.TestCase):
    def setUp(self):
        allowed_workflows = json.dumps(
            {
                "workflow1": {
                    "uri": "https://example.com/api",
                    "method": "GET",
                    "auth_type": "HTTP_BASIC",
                    "secret_name": (
                        "projects/123456789012/secrets/api-secret/versions/latest"
                    ),
                    "auth_server": None,
                    "client_id": None,
                }
            }
        )
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
            patch.object(config, "ALLOWED_WORKFLOWS", allowed_workflows),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_default_source_idor_does_not_enqueue_or_write_bq(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # No "source" key -> Handler defaults to BIGQUERY_ROUTINE.
        req = _FakeFlaskRequest(
            {
                "calls": [
                    _make_call_row(
                        "projects/victim-project/locations/us-central1/queues/victim-queue"
                    ),
                    _make_call_row(
                        "projects/gdp-test-lod/locations/us-west1/queues/unrelated-queue"
                    ),
                ]
            }
        )
        body, status = Handler.execute(req)

        self.assertEqual(status, 200)
        replies = json.loads(body)["replies"]
        self.assertEqual(len(replies), 2)
        for reply in replies:
            self.assertEqual(reply, {"error": "Invalid queue_name parameter."})
        mock_client.create_task.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_valid_call_enqueues_with_oidc_token_and_deterministic_run_app_url(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client.create_task.return_value = MagicMock(name="created-task-1")
        mock_client_cls.return_value = mock_client

        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        req = _FakeFlaskRequest({"calls": [_make_call_row(valid_queue)]})
        body, status = Handler.execute(req)

        self.assertEqual(status, 200)
        replies = json.loads(body)["replies"]
        self.assertEqual(replies, [{"response": "Request added to the queue."}])

        mock_client.create_task.assert_called_once()
        task_req = mock_client.create_task.call_args.kwargs["request"]
        self.assertEqual(task_req["parent"], valid_queue)

        http_req = task_req["task"].http_request
        expected_uri = "https://load-0-api-fnc-123456789012.us-west1.run.app"
        self.assertEqual(http_req.url, expected_uri)
        self.assertIsNotNone(http_req.oidc_token)
        self.assertEqual(
            http_req.oidc_token.service_account_email,
            "api-src-fnc-sa-invoker@gdp-test-lod.iam.gserviceaccount.com",
        )
        self.assertEqual(http_req.oidc_token.audience, expected_uri)

        enqueued_payload = json.loads(http_req.body.decode("utf-8"))
        self.assertEqual(enqueued_payload["source"], "CLOUD_TASK")
        self.assertEqual(enqueued_payload["query_string"], "q1=val1")
        mock_save_bq.assert_called_once()

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_fails_closed_when_task_invoker_sa_missing(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        with patch.object(config, "TASK_INVOKER_SA", None):
            req = _FakeFlaskRequest({"calls": [_make_call_row(valid_queue)]})
            body, status = Handler.execute(req)

        self.assertEqual(status, 200)
        replies = json.loads(body)["replies"]
        self.assertEqual(
            replies, [{"error": "Server configuration error for task dispatch."}]
        )
        mock_client.create_task.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_mixed_batch_rejects_bad_row_and_enqueues_valid_row(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client.create_task.return_value = MagicMock(name="created-task-2")
        mock_client_cls.return_value = mock_client

        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        req = _FakeFlaskRequest(
            {
                "calls": [
                    _make_call_row("projects/evil/locations/us-west1/queues/cloud-task-api-1"),
                    ["only-one-arg"],
                    _make_call_row(valid_queue),
                ]
            }
        )
        body, status = Handler.execute(req)

        self.assertEqual(status, 200)
        replies = json.loads(body)["replies"]
        self.assertEqual(len(replies), 3)
        self.assertIn("error", replies[0])
        self.assertIn("error", replies[1])
        self.assertEqual(replies[2], {"response": "Request added to the queue."})
        mock_client.create_task.assert_called_once()
        self.assertEqual(
            mock_client.create_task.call_args.kwargs["request"]["parent"], valid_queue
        )

    def test_function_uri_override_requires_https_and_strips_trailing_slash(self):
        with patch.object(
            config,
            "FUNCTION_URI",
            "https://load-0-api-fnc-abc123-uw.a.run.app/",
        ):
            self.assertEqual(
                BigQueryRoutineRequest._build_function_uri(),
                "https://load-0-api-fnc-abc123-uw.a.run.app",
            )

        with patch.object(
            config,
            "FUNCTION_URI",
            "http://insecure.example.com",
        ):
            with self.assertRaises(ValueError):
                BigQueryRoutineRequest._build_function_uri()

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_create_task_error_does_not_leak_internal_exception_details(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client.create_task.side_effect = RuntimeError("internal-secret-stack-info")
        mock_client_cls.return_value = mock_client

        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        req = _FakeFlaskRequest({"calls": [_make_call_row(valid_queue)]})
        body, status = Handler.execute(req)

        self.assertEqual(status, 200)
        self.assertNotIn("internal-secret-stack-info", body)
        self.assertEqual(
            json.loads(body)["replies"],
            [{"error": "Error adding request to the queue."}],
        )


class TestTerraformApiConnectorSecurity(unittest.TestCase):
    def setUp(self):
        self.tf_text = TF_API_CONNECTOR.read_text(encoding="utf-8")

    def test_no_public_invoker_or_allow_all_ingress(self):
        self.assertNotIn('"allUsers"', self.tf_text)
        self.assertNotIn('"allAuthenticatedUsers"', self.tf_text)
        self.assertNotIn('"ALLOW_ALL"', self.tf_text)
        self.assertIn('ingress_settings = "ALLOW_INTERNAL_ONLY"', self.tf_text)

    def test_function_scoped_invoker_restricted_to_bq_and_cloud_tasks_sa(self):
        self.assertIn(
            '"serviceAccount:${google_bigquery_connection.load-connection-bq-0.cloud_resource[0].service_account_id}"',
            self.tf_text,
        )
        self.assertIn(
            '"serviceAccount:${google_service_account.load-0-api-fnc-invoker-sa.email}"',
            self.tf_text,
        )
        # No authoritative project-level IAM binding clobbering run.invoker.
        self.assertNotIn('resource "google_project_iam_binding"', self.tf_text)

    def test_runner_sa_act_as_scoped_to_invoker_sa_only(self):
        runner_block = re.search(
            r'resource "google_project_iam_member" "load-0-api-fnc-runner-sa-iam"\s*\{[^}]*\}',
            self.tf_text,
            re.DOTALL,
        )
        self.assertIsNotNone(runner_block)
        self.assertNotIn("roles/iam.serviceAccountUser", runner_block.group(0))

        self.assertIn(
            'resource "google_service_account_iam_member" "load-0-api-fnc-runner-act-as-invoker"',
            self.tf_text,
        )
        self.assertIn(
            "service_account_id = google_service_account.load-0-api-fnc-invoker-sa.name",
            self.tf_text,
        )

    def test_function_env_configures_task_invoker_sa_and_queue_prefix(self):
        self.assertIn(
            "TASK_INVOKER_SA      = google_service_account.load-0-api-fnc-invoker-sa.email",
            self.tf_text,
        )
        self.assertIn('ALLOWED_QUEUE_PREFIX = "cloud-task-api-"', self.tf_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
