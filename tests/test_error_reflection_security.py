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

"""Regression guards for CWE-209 / CWE-200 internal exception reflection and
GCP resource existence/enumeration oracles (Secret Manager, Cloud Tasks,
BigQuery, Pub/Sub) across Cloud Function HTTP entrypoints and inner handlers.
"""

import ast
import importlib.util
import json
import os
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
WORKFLOW_POSTPROCESS_DIR = REPO_ROOT / "workflow-postprocess"
WORKFLOW1_DIR = WORKFLOW_POSTPROCESS_DIR / "workflow1"

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
    """Install or augment minimal stubs when third-party CF deps are absent."""
    if "coloredlogs" not in sys.modules:
        try:
            import coloredlogs  # noqa: F401
        except ImportError:
            mod = types.ModuleType("coloredlogs")
            mod.install = lambda *args, **kwargs: None
            sys.modules["coloredlogs"] = mod

    if "functions_framework" not in sys.modules:
        try:
            import functions_framework  # noqa: F401
        except ImportError:
            mod = types.ModuleType("functions_framework")
            mod.http = lambda fn: fn
            sys.modules["functions_framework"] = mod

    try:
        import flask  # noqa: F401
    except ImportError:
        flask = types.ModuleType("flask")
        sys.modules["flask"] = flask
    else:
        flask = sys.modules["flask"]

    if not hasattr(flask, "Request"):

        class _Request:
            pass

        flask.Request = _Request

    if not hasattr(flask, "Response"):

        class _Response:
            def __init__(self, response: Any = ""):
                self._data = (
                    response.encode("utf-8")
                    if isinstance(response, str)
                    else bytes(response)
                )

            def get_data(self, as_text: bool = False) -> Any:
                return self._data.decode("utf-8") if as_text else self._data

        flask.Response = _Response

    if "flask.typing" not in sys.modules:
        try:
            import flask.typing  # noqa: F401
        except ImportError:
            typing_mod = types.ModuleType("flask.typing")
            typing_mod.ResponseReturnValue = Any
            flask.typing = typing_mod
            sys.modules["flask.typing"] = typing_mod

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
        if sub == "secretmanager" and not hasattr(mod, "SecretManagerServiceClient"):

            class SecretManagerServiceClient:
                def access_secret_version(self, name=None):
                    raise RuntimeError("SecretManagerServiceClient must be mocked.")

            mod.SecretManagerServiceClient = SecretManagerServiceClient


def _load_cf_main(package_name: str, directory: Path) -> types.ModuleType:
    """Import a Cloud Function's main.py using its package-relative imports."""
    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(directory)]
        sys.modules[package_name] = pkg
    mod_name = f"{package_name}.main"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, directory / "main.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_install_optional_stubs()
API_MAIN = _load_cf_main("_gdp_api_connector_cf", API_CONNECTOR_DIR)
WF1_MAIN = _load_cf_main("_gdp_workflow1_cf", WORKFLOW1_DIR)

API_CONFIG = sys.modules["_gdp_api_connector_cf.src.config"].config
API_BQ_MOD = sys.modules["_gdp_api_connector_cf.src.handlers.bigquery"]
API_CT_MOD = sys.modules["_gdp_api_connector_cf.src.handlers.cloud_task"]
API_POLICY_MOD = sys.modules["_gdp_api_connector_cf.src.workflow_policy"]

PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

ALLOWED_WORKFLOWS_FIXTURE = json.dumps(
    {
        "workflow1": {
            "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
            "method": "GET",
            "auth_type": "HTTP_BASIC",
            "secret_name": (
                "projects/123456789012/secrets/workflow1-basic/versions/latest"
            ),
            "auth_server": None,
            "client_id": None,
        },
    }
)


class _FakeFlaskRequest:
    def __init__(self, payload: Any):
        self._payload = payload

    def get_json(self, silent: bool = False) -> Any:
        return self._payload


def _response_text(response: Any) -> str:
    return response.get_data(as_text=True)


class TestTopLevelMainErrorReflectionCWE209(unittest.TestCase):
    """Smallest reproductions for top-level Cloud Function catch-all handlers."""

    SENSITIVE_GCP_ERRORS = [
        (
            "secret_not_found",
            RuntimeError(
                "404 Secret [projects/prod-secret-proj-999/secrets/db-password/versions/latest] not found."
            ),
            "prod-secret-proj-999",
        ),
        (
            "queue_not_found",
            RuntimeError(
                "404 Queue [projects/prod-tasks-proj-888/locations/us-west1/queues/cloud-task-api-1] not found."
            ),
            "prod-tasks-proj-888",
        ),
        (
            "bq_table_not_found",
            RuntimeError(
                "404 Not found: Table prod-bq-proj-777:gdp_dwh_load_bq_0.tbl_result_20250101"
            ),
            "prod-bq-proj-777",
        ),
        (
            "pubsub_topic_not_found",
            RuntimeError(
                "404 Resource not found (resource=projects/prod-trf-proj-666/topics/gdp-wf-trf-ps-0)."
            ),
            "prod-trf-proj-666",
        ),
    ]

    def test_api_connector_main_never_reflects_exception_details_and_logs_server_side(
        self,
    ):
        for label, exc, secret_marker in self.SENSITIVE_GCP_ERRORS:
            with self.subTest(case=label):
                with patch.object(API_MAIN.Handler, "execute", side_effect=exc), patch.object(
                    API_MAIN, "logger"
                ) as mock_logger:
                    resp, status = API_MAIN.main(_FakeFlaskRequest({"source": "CLOUD_TASK"}))

                body = _response_text(resp)
                self.assertEqual(status, 500)
                self.assertEqual(body, "Internal server error.\n")
                self.assertNotIn(secret_marker, body)
                self.assertNotIn("Exception occurred", body)
                self.assertNotIn("projects/", body)

                mock_logger.error.assert_called_once()
                self.assertIn(secret_marker, str(mock_logger.error.call_args.args[0]))

    def test_workflow_postprocess_main_never_reflects_exception_details_and_logs_server_side(
        self,
    ):
        for label, exc, secret_marker in self.SENSITIVE_GCP_ERRORS:
            with self.subTest(case=label):
                with patch.object(WF1_MAIN.Handler, "execute", side_effect=exc), patch.object(
                    WF1_MAIN, "logger"
                ) as mock_logger:
                    resp, status = WF1_MAIN.main(_FakeFlaskRequest({"message": {}}))

                body = _response_text(resp)
                self.assertEqual(status, 500)
                self.assertEqual(body, "Internal server error.\n")
                self.assertNotIn(secret_marker, body)
                self.assertNotIn("Exception occurred", body)
                self.assertNotIn("projects/", body)

                mock_logger.error.assert_called_once()
                self.assertIn(secret_marker, str(mock_logger.error.call_args.args[0]))


class TestInnerHandlerEnumerationOracleGuards(unittest.TestCase):
    """End-to-end tests through api-connector/main.py for inner handler error paths."""

    def setUp(self):
        self._patches = [
            patch.object(API_CONFIG, "PROJECT_ID", "gdp-test-lod"),
            patch.object(API_CONFIG, "PROJECT_NUMBER", "123456789012"),
            patch.object(API_CONFIG, "REGION", "us-west1"),
            patch.object(API_CONFIG, "FUNCTION_NAME", "load-0-api-fnc"),
            patch.object(API_CONFIG, "FUNCTION_URI", None),
            patch.object(
                API_CONFIG,
                "TASK_INVOKER_SA",
                "api-src-fnc-sa-invoker@gdp-test-lod.iam.gserviceaccount.com",
            ),
            patch.object(API_CONFIG, "ALLOWED_QUEUE_PREFIX", "cloud-task-api-"),
            patch.object(API_CONFIG, "ALLOWED_WORKFLOWS", ALLOWED_WORKFLOWS_FIXTURE),
            patch.object(API_CONFIG, "PUBSUB_TOPICS", None),
            patch.object(
                API_POLICY_MOD.socket, "getaddrinfo", return_value=PUBLIC_ADDRINFO
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_secret_manager_404_and_403_return_identical_generic_response(self):
        """Secret Manager errors must not reveal resource paths or act as a
        404-vs-403 existence oracle.
        """
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": (
                        "projects/123456789012/secrets/workflow1-basic/versions/latest"
                    ),
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )

        errors = [
            RuntimeError(
                "404 Secret [projects/prod-proj-id/secrets/workflow1-basic/versions/latest] not found."
            ),
            RuntimeError(
                "403 Permission 'secretmanager.versions.access' denied for resource 'projects/prod-proj-id/secrets/workflow1-basic/versions/latest'."
            ),
        ]
        responses = []
        for err in errors:
            with patch.object(
                API_CT_MOD.Secrets, "get_value", side_effect=err
            ), patch.object(API_CT_MOD, "logger") as mock_logger:
                resp, status = API_MAIN.main(req)
                body = _response_text(resp)
                responses.append((status, body))
                self.assertNotIn("prod-proj-id", body)
                self.assertNotIn("404", body)
                self.assertNotIn("403", body)
                self.assertNotIn("projects/", body)
                mock_logger.error.assert_called()

        self.assertEqual(responses[0], (202, "Could not retrieve secret.\n"))
        self.assertEqual(responses[0], responses[1])

    def test_cloud_tasks_404_and_403_return_identical_generic_response(self):
        """Cloud Tasks errors must not leak internal project IDs, queue paths,
        or distinguish missing queues from permission errors.
        """
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101"
        )
        row = [
            "workflow1",
            json.dumps(
                {"uri": "https://pokeapi.co/api/v2/pokemon/ditto", "method": "GET"}
            ),
            json.dumps(
                {
                    "type": "HTTP_BASIC",
                    "secret_name": (
                        "projects/123456789012/secrets/workflow1-basic/versions/latest"
                    ),
                }
            ),
            json.dumps({"Content-Type": "application/json"}),
            json.dumps({"q1": "val1"}),
            json.dumps({"k1": "val1"}),
            "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            valid_queue,
        ]
        req = _FakeFlaskRequest({"calls": [row]})

        errors = [
            RuntimeError(
                f"404 Queue [{valid_queue}] does not exist in project prod-tasks-id."
            ),
            RuntimeError(
                f"403 Permission 'cloudtasks.tasks.create' denied on [{valid_queue}] in prod-tasks-id."
            ),
        ]
        responses = []
        for err in errors:
            mock_client = MagicMock()
            mock_client.create_task.side_effect = err
            with patch.object(
                API_BQ_MOD.tasks_v2, "CloudTasksClient", return_value=mock_client
            ), patch.object(
                API_BQ_MOD.Utils, "save_bigquery", return_value=True
            ), patch.object(API_BQ_MOD, "logger") as mock_logger:
                resp, status = API_MAIN.main(req)
                body = _response_text(resp)
                responses.append((status, body))
                self.assertNotIn("prod-tasks-id", body)
                self.assertNotIn("404", body)
                self.assertNotIn("403", body)
                self.assertNotIn(valid_queue, body)
                mock_logger.error.assert_called()

        self.assertEqual(responses[0][0], 200)
        self.assertEqual(
            json.loads(responses[0][1]),
            {"replies": [{"error": "Error adding request to the queue."}]},
        )
        self.assertEqual(responses[0], responses[1])

    def test_bigquery_table_not_found_does_not_leak_resource_path(self):
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101"
        )
        row = [
            "workflow1",
            json.dumps(
                {"uri": "https://pokeapi.co/api/v2/pokemon/ditto", "method": "GET"}
            ),
            json.dumps(
                {
                    "type": "HTTP_BASIC",
                    "secret_name": (
                        "projects/123456789012/secrets/workflow1-basic/versions/latest"
                    ),
                }
            ),
            "{}",
            "{}",
            "{}",
            "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            valid_queue,
        ]
        mock_client = MagicMock()
        mock_client.create_task.return_value = MagicMock(name="task-1")
        bq_err = RuntimeError(
            "404 Not found: Table prod-bq-proj:gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000"
        )
        with patch.object(
            API_BQ_MOD.tasks_v2, "CloudTasksClient", return_value=mock_client
        ), patch.object(
            API_BQ_MOD.Utils, "save_bigquery", side_effect=bq_err
        ):
            resp, status = API_MAIN.main(_FakeFlaskRequest({"calls": [row]}))

        body = _response_text(resp)
        self.assertEqual(status, 500)
        self.assertEqual(body, "Internal server error.\n")
        self.assertNotIn("prod-bq-proj", body)
        self.assertNotIn("tbl_process_log", body)

    def test_handler_does_not_reflect_untrusted_source_or_misroute_inner_keyerror(self):
        # Untrusted source value must not be reflected in 422 response.
        probe = "ATTACKER_PROBE_projects/123456/secrets/foo"
        resp, status = API_MAIN.main(_FakeFlaskRequest({"source": probe}))
        body = _response_text(resp)
        self.assertEqual(status, 422)
        self.assertEqual(body, "Unsupported request source.\n")
        self.assertNotIn(probe, body)

        # Non-string / unhashable source returns 422 cleanly without 500 or reflection.
        resp_list, status_list = API_MAIN.main(_FakeFlaskRequest({"source": ["bad"]}))
        self.assertEqual(status_list, 422)
        self.assertEqual(_response_text(resp_list), "Unsupported request source.\n")

        # Missing 'calls' raises inner KeyError, which must NOT be misclassified as
        # unsupported source and must NOT leak the missing key name to callers.
        resp_missing, status_missing = API_MAIN.main(
            _FakeFlaskRequest({"source": "BIGQUERY_ROUTINE"})
        )
        body_missing = _response_text(resp_missing)
        self.assertEqual(status_missing, 500)
        self.assertEqual(body_missing, "Internal server error.\n")
        self.assertNotIn("calls", body_missing)
        self.assertNotIn("not supported", body_missing)

        # Non-dict JSON payload returns generic 400.
        resp_arr, status_arr = API_MAIN.main(_FakeFlaskRequest(["not", "an", "object"]))
        self.assertEqual(status_arr, 400)
        self.assertEqual(_response_text(resp_arr), "Invalid request.\n")


class TestStaticNoExceptionReflectionInCloudFunctions(unittest.TestCase):
    """AST guard preventing any Cloud Function entrypoint from reflecting
    caught exception variables in HTTP responses.
    """

    def test_no_cf_main_interpolates_exception_into_flask_response(self):
        main_files = [API_CONNECTOR_DIR / "main.py"] + sorted(
            WORKFLOW_POSTPROCESS_DIR.glob("*/main.py")
        )
        self.assertGreaterEqual(len(main_files), 2)

        for path in main_files:
            with self.subTest(file=str(path.relative_to(REPO_ROOT))):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ExceptHandler):
                        continue
                    exc_name = node.name
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "Response"
                        ):
                            for arg in child.args:
                                # Disallow f-strings or referencing the caught exception
                                # variable in flask.Response(...) inside an except block.
                                self.assertNotIsInstance(
                                    arg,
                                    ast.JoinedStr,
                                    f"{path}: flask.Response inside except must return a static string.",
                                )
                                if exc_name:
                                    used_names = {
                                        n.id
                                        for n in ast.walk(arg)
                                        if isinstance(n, ast.Name)
                                    }
                                    self.assertNotIn(
                                        exc_name,
                                        used_names,
                                        f"{path}: flask.Response must not reference caught exception '{exc_name}'.",
                                    )

    def test_no_handler_except_block_returns_or_appends_caught_exception(self):
        handler_files = sorted(
            (API_CONNECTOR_DIR / "src").glob("handler.py")
        ) + sorted(
            (API_CONNECTOR_DIR / "src" / "handlers").glob("*.py")
        ) + sorted(
            WORKFLOW_POSTPROCESS_DIR.glob("*/src/*.py")
        )
        self.assertGreaterEqual(len(handler_files), 4)

        for path in handler_files:
            with self.subTest(file=str(path.relative_to(REPO_ROOT))):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ExceptHandler) or not node.name:
                        continue
                    exc_name = node.name
                    for child in ast.walk(node):
                        if isinstance(child, ast.Return) and child.value is not None:
                            used = {
                                n.id
                                for n in ast.walk(child.value)
                                if isinstance(n, ast.Name)
                            }
                            self.assertNotIn(
                                exc_name,
                                used,
                                f"{path}: return inside except must not leak '{exc_name}'.",
                            )
                        if (
                            isinstance(child, ast.Assign)
                            and any(
                                isinstance(t, ast.Name) and t.id == "log_info"
                                for t in child.targets
                            )
                        ):
                            used = {
                                n.id
                                for n in ast.walk(child.value)
                                if isinstance(n, ast.Name)
                            }
                            self.assertNotIn(
                                exc_name,
                                used,
                                f"{path}: client-facing log_info inside except must not leak '{exc_name}'.",
                            )
                        if (
                            isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "append"
                            and isinstance(child.func.value, ast.Name)
                            and child.func.value.id == "replies"
                        ):
                            for arg in child.args:
                                used = {
                                    n.id
                                    for n in ast.walk(arg)
                                    if isinstance(n, ast.Name)
                                }
                                self.assertNotIn(
                                    exc_name,
                                    used,
                                    f"{path}: replies.append inside except must not leak '{exc_name}'.",
                                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
