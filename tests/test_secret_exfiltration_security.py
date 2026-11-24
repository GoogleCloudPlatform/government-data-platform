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

"""Regression guards for CWE-200 Secret Manager credential exfiltration
(confused deputy) in api-connector CLOUD_TASK / BIGQUERY_ROUTINE handlers
and Terraform Secret Manager IAM hardening in 1-foundations/02-api-connector.tf.
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
FOUNDATIONS_DIR = REPO_ROOT / "1-foundations"
TF_API_CONNECTOR = FOUNDATIONS_DIR / "02-api-connector.tf"

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
        if sub == "secretmanager" and not hasattr(mod, "SecretManagerServiceClient"):

            class SecretManagerServiceClient:
                def access_secret_version(self, name=None):
                    raise RuntimeError("SecretManagerServiceClient must be mocked.")

            mod.SecretManagerServiceClient = SecretManagerServiceClient


_install_optional_stubs()
if str(API_CONNECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(API_CONNECTOR_DIR))

from src.config import config  # noqa: E402
from src.gsecrets import Secrets  # noqa: E402
from src.handler import Handler  # noqa: E402
from src.workflow_policy import validate_https_url, validate_secret_name  # noqa: E402


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


ALLOWED_WORKFLOWS_FIXTURE = json.dumps(
    {
        "workflow1": {
            "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
            "method": "GET",
            "auth_type": "HTTP_BASIC",
            "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
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
        "public_workflow": {
            "uri": "https://data.public.example/v1/items",
            "method": "GET",
            "auth_type": "NONE",
            "secret_name": None,
            "auth_server": None,
            "client_id": None,
        },
    }
)


class TestCloudTaskSecretExfiltrationCWE200(unittest.TestCase):
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
                "src.workflow_policy.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
                ],
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_blocks_http_basic_secret_exfil_to_attacker_uri(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        """Smallest reproduction of the reported CWE-200 exploit: attacker-chosen
        URI paired with a valid or arbitrary Secret Manager secret_name.
        """
        attack_uris = [
            "https://attacker.example/exfil",
            "https://pokeapi.co.attacker.example/api/v2/pokemon/ditto",
            "https://pokeapi.co@attacker.example/api/v2/pokemon/ditto",
            "http://pokeapi.co/api/v2/pokemon/ditto",
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
        ]
        for uri in attack_uris:
            with self.subTest(uri=uri):
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
                        "request_config": {"uri": uri, "method": "GET"},
                        "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                    }
                )
                body, status = Handler.execute(req)
                self.assertEqual(status, 403)
                self.assertEqual(body, "Unauthorized workflow request.")

        mock_get_secret.assert_not_called()
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_blocks_http_basic_unauthorized_secret_name(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        unauthorized_secrets = [
            "projects/123456789012/secrets/other-prod-secret/versions/latest",
            "projects/999999999999/secrets/workflow1-basic/versions/latest",
            "projects/123456789012/secrets/workflow1-basic/versions/2",
            "projects/123456789012/secrets/../../victim/versions/latest",
            "projects/123456789012/secrets/workflow1-basic/versions/latest\n",
            "",
            None,
        ]
        for secret_name in unauthorized_secrets:
            with self.subTest(secret_name=secret_name):
                req = _FakeFlaskRequest(
                    {
                        "source": "CLOUD_TASK",
                        "workflow_id": "workflow1",
                        "auth": {
                            "type": "HTTP_BASIC",
                            "secret_name": secret_name,
                        },
                        "request_config": {
                            "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                            "method": "GET",
                        },
                        "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                    }
                )
                body, status = Handler.execute(req)
                self.assertEqual(status, 403)
                self.assertEqual(body, "Unauthorized workflow request.")

        mock_get_secret.assert_not_called()
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.post")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_blocks_client_credentials_exfil_via_attacker_auth_server_or_uri(
        self, mock_get_secret, mock_requests_post, mock_save_bq
    ):
        attacks = [
            # Attacker auth_server to steal client_secret
            {
                "auth": {
                    "type": "CLIENT_CREDENTIALS",
                    "auth_server": "https://attacker.example/oauth2/token",
                    "client_id": "gdp-client-id",
                    "secret_name": "projects/123456789012/secrets/oauth-client/versions/1",
                },
                "request_config": {
                    "uri": "https://api.partner.example/v1/records",
                    "method": "POST",
                },
            },
            # Attacker target URI to steal access_token
            {
                "auth": {
                    "type": "CLIENT_CREDENTIALS",
                    "auth_server": "https://auth.partner.example/oauth2/token",
                    "client_id": "gdp-client-id",
                    "secret_name": "projects/123456789012/secrets/oauth-client/versions/1",
                },
                "request_config": {
                    "uri": "https://attacker.example/steal-token",
                    "method": "POST",
                },
            },
            # Swapped secret_name
            {
                "auth": {
                    "type": "CLIENT_CREDENTIALS",
                    "auth_server": "https://auth.partner.example/oauth2/token",
                    "client_id": "gdp-client-id",
                    "secret_name": "projects/123456789012/secrets/other-secret/versions/latest",
                },
                "request_config": {
                    "uri": "https://api.partner.example/v1/records",
                    "method": "POST",
                },
            },
            # Swapped client_id
            {
                "auth": {
                    "type": "CLIENT_CREDENTIALS",
                    "auth_server": "https://auth.partner.example/oauth2/token",
                    "client_id": "attacker-client-id",
                    "secret_name": "projects/123456789012/secrets/oauth-client/versions/1",
                },
                "request_config": {
                    "uri": "https://api.partner.example/v1/records",
                    "method": "POST",
                },
            },
        ]
        for case in attacks:
            with self.subTest(case=case):
                req = _FakeFlaskRequest(
                    {
                        "source": "CLOUD_TASK",
                        "workflow_id": "oauth_workflow",
                        "auth": case["auth"],
                        "request_config": case["request_config"],
                        "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
                    }
                )
                body, status = Handler.execute(req)
                self.assertEqual(status, 403)
                self.assertEqual(body, "Unauthorized workflow request.")

        mock_get_secret.assert_not_called()
        mock_requests_post.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_blocks_unknown_workflow_and_method_or_auth_type_mismatch(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        bad_requests = [
            # Unknown workflow_id
            {
                "source": "CLOUD_TASK",
                "workflow_id": "attacker-workflow",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            },
            # Missing workflow_id
            {
                "source": "CLOUD_TASK",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            },
            # HTTP method mismatch (POST instead of GET)
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "POST",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            },
            # Escalating auth on NONE workflow
            {
                "source": "CLOUD_TASK",
                "workflow_id": "public_workflow",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://data.public.example/v1/items",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            },
            # Passing secret_name on NONE workflow
            {
                "source": "CLOUD_TASK",
                "workflow_id": "public_workflow",
                "auth": {
                    "type": "NONE",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://data.public.example/v1/items",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            },
        ]
        for payload in bad_requests:
            with self.subTest(payload=payload):
                body, status = Handler.execute(_FakeFlaskRequest(payload))
                self.assertEqual(status, 403)
                self.assertEqual(body, "Unauthorized workflow request.")

        mock_get_secret.assert_not_called()
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_fails_closed_when_allowed_workflows_missing_or_unsafe(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        bad_policies = [
            None,
            "",
            "{}",
            "not-valid-json",
            json.dumps(
                {
                    "workflow1": {
                        "uri": "http://insecure.example/api",
                        "method": "GET",
                        "auth_type": "HTTP_BASIC",
                        "secret_name": "projects/123456789012/secrets/s/versions/latest",
                    }
                }
            ),
            json.dumps(
                {
                    "workflow1": {
                        "uri": "https://169.254.169.254/computeMetadata/v1/",
                        "method": "GET",
                        "auth_type": "HTTP_BASIC",
                        "secret_name": "projects/123456789012/secrets/s/versions/latest",
                    }
                }
            ),
        ]
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/s/versions/latest",
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        for policy_val in bad_policies:
            with self.subTest(policy=policy_val):
                with patch.object(config, "ALLOWED_WORKFLOWS", policy_val):
                    body, status = Handler.execute(req)
                    self.assertEqual(status, 503)
                    self.assertEqual(
                        body, "Server configuration error for workflow policy."
                    )

        mock_get_secret.assert_not_called()
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_authorized_http_basic_executes_without_redirects_and_strips_sensitive_headers(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        mock_get_secret.return_value = json.dumps(
            {"username": "apiuser", "password": "apipassword"}
        )
        mock_requests_get.return_value = _make_mock_http_response(
            200, '{"name": "ditto"}'
        )

        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "GET",
                    "timeout": 15,
                },
                "headers": {
                    "Content-Type": "application/json",
                    "KEY": "api123",
                    "Host": "attacker.example",
                    "Authorization": "Basic spoofed",
                    "Proxy-Authorization": "Basic spoofed",
                },
                "query_string": "q1=val1",
                "result_table": "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
            }
        )

        body, status = Handler.execute(req)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"status": "processed", "status_code": 200})

        mock_get_secret.assert_called_once_with(
            "projects/123456789012/secrets/workflow1-basic/versions/latest"
        )
        mock_requests_get.assert_called_once()
        args, kwargs = mock_requests_get.call_args
        self.assertEqual(args[0], "https://pokeapi.co/api/v2/pokemon/ditto")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["auth"], ("apiuser", "apipassword"))
        self.assertEqual(
            kwargs["headers"], {"Content-Type": "application/json", "KEY": "api123"}
        )
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(mock_save_bq.call_count, 2)
        self.assertEqual(
            mock_save_bq.call_args_list[0].args[1]["response"]["body"],
            '{"name": "ditto"}',
        )

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.post")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_authorized_client_credentials_executes_without_redirects(
        self, mock_get_secret, mock_requests_post, mock_save_bq
    ):
        mock_get_secret.return_value = json.dumps({"client_secret": "s3cr3t"})
        token_resp = _make_mock_http_response(200, '{"access_token": "tok-123"}')
        api_resp = _make_mock_http_response(200, '{"items": []}')
        mock_requests_post.side_effect = [token_resp, api_resp]

        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "oauth_workflow",
                "auth": {
                    "type": "CLIENT_CREDENTIALS",
                    "auth_server": "https://auth.partner.example/oauth2/token",
                    "client_id": "gdp-client-id",
                    "secret_name": "projects/123456789012/secrets/oauth-client/versions/1",
                },
                "request_config": {
                    "uri": "https://api.partner.example/v1/records",
                    "method": "POST",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )

        body, status = Handler.execute(req)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"status": "processed", "status_code": 200})
        self.assertEqual(mock_requests_post.call_count, 2)
        self.assertEqual(
            mock_save_bq.call_args_list[0].args[1]["response"]["body"], '{"items": []}'
        )

        token_call = mock_requests_post.call_args_list[0]
        self.assertEqual(
            token_call.kwargs["url"], "https://auth.partner.example/oauth2/token"
        )
        self.assertFalse(token_call.kwargs["allow_redirects"])
        self.assertEqual(token_call.kwargs["auth"], ("gdp-client-id", "s3cr3t"))
        self.assertEqual(token_call.kwargs["timeout"], 10)

        api_call = mock_requests_post.call_args_list[1]
        self.assertEqual(api_call.args[0], "https://api.partner.example/v1/records")
        self.assertFalse(api_call.kwargs["allow_redirects"])

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_authorized_none_auth_skips_secret_manager(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        mock_requests_get.return_value = _make_mock_http_response(200, '{"ok": true}')
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "public_workflow",
                "request_config": {
                    "uri": "https://data.public.example/v1/items",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 202)
        mock_get_secret.assert_not_called()
        mock_requests_get.assert_called_once()
        self.assertIsNone(mock_requests_get.call_args.kwargs["auth"])
        self.assertFalse(mock_requests_get.call_args.kwargs["allow_redirects"])

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_rejects_invalid_result_table_before_touching_secrets(
        self, mock_get_secret, mock_requests_get, mock_save_bq
    ):
        for bad_table in (
            None,
            "",
            "other-proj.dataset.tbl_result_1",
            "dataset_only",
            "dataset.tbl_result_1\n",
        ):
            with self.subTest(result_table=bad_table):
                req = _FakeFlaskRequest(
                    {
                        "source": "CLOUD_TASK",
                        "workflow_id": "workflow1",
                        "auth": {
                            "type": "HTTP_BASIC",
                            "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                        },
                        "request_config": {
                            "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                            "method": "GET",
                        },
                        "result_table": bad_table,
                    }
                )
                body, status = Handler.execute(req)
                self.assertEqual(status, 400)
                self.assertEqual(body, "Invalid result_table parameter.")

        mock_get_secret.assert_not_called()
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_secret_error_does_not_leak_internal_details(self, mock_get_secret):
        mock_get_secret.side_effect = RuntimeError("sensitive-internal-secret-path")
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "auth": {
                    "type": "HTTP_BASIC",
                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                },
                "request_config": {
                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                    "method": "GET",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 202)
        self.assertNotIn("sensitive-internal-secret-path", body)
        self.assertEqual(body, "Could not retrieve secret.")


class TestBigQueryRoutineExfiltrationDefenseInDepth(unittest.TestCase):
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

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_bq_routine_blocks_attacker_uri_and_secret_before_enqueuing(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        valid_table = "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000"

        req = _FakeFlaskRequest(
            {
                "calls": [
                    # Attacker URI with valid secret
                    [
                        "workflow1",
                        json.dumps({"uri": "https://attacker.example/exfil", "method": "GET"}),
                        json.dumps(
                            {
                                "type": "HTTP_BASIC",
                                "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                            }
                        ),
                        "{}",
                        "{}",
                        "{}",
                        valid_table,
                        valid_queue,
                    ],
                    # Valid URI with unauthorized secret
                    [
                        "workflow1",
                        json.dumps(
                            {
                                "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                                "method": "GET",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "HTTP_BASIC",
                                "secret_name": "projects/123456789012/secrets/victim-secret/versions/latest",
                            }
                        ),
                        "{}",
                        "{}",
                        "{}",
                        valid_table,
                        valid_queue,
                    ],
                ]
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 200)
        replies = json.loads(body)["replies"]
        self.assertEqual(
            replies,
            [
                {"error": "Unauthorized workflow request."},
                {"error": "Unauthorized workflow request."},
            ],
        )
        mock_client.create_task.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_bq_routine_fails_closed_when_allowed_workflows_missing(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        with patch.object(config, "ALLOWED_WORKFLOWS", None):
            req = _FakeFlaskRequest(
                {
                    "calls": [
                        [
                            "workflow1",
                            json.dumps(
                                {
                                    "uri": "https://pokeapi.co/api/v2/pokemon/ditto",
                                    "method": "GET",
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "HTTP_BASIC",
                                    "secret_name": "projects/123456789012/secrets/workflow1-basic/versions/latest",
                                }
                            ),
                            "{}",
                            "{}",
                            "{}",
                            "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000",
                            valid_queue,
                        ]
                    ]
                }
            )
            body, status = Handler.execute(req)

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["replies"],
            [{"error": "Server configuration error for workflow policy."}],
        )
        mock_client.create_task.assert_not_called()
        mock_save_bq.assert_not_called()


class TestValidatorsDefenseInDepth(unittest.TestCase):
    def test_validate_https_url_rejects_ssrf_and_non_https(self):
        bad_urls = [
            "http://example.com/api",
            "https://169.254.169.254/computeMetadata/v1/",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://metadata.goog/",
            "https://localhost/admin",
            "https://sub.localhost/admin",
            "https://127.0.0.1/admin",
            "https://10.0.0.1/internal",
            "https://192.168.1.1/internal",
            "https://[::1]/internal",
            "https://[::ffff:169.254.169.254]/computeMetadata/v1/",
            "https://trusted.example@attacker.example/api",
            "https://example.com/api\n",
            "",
            None,
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_https_url(url)

    @patch("src.gsecrets.secretmanager.SecretManagerServiceClient")
    def test_secrets_get_value_rejects_malformed_paths_before_api_call(
        self, mock_sm_client
    ):
        bad_names = [
            "projects/123/secrets/../../other/versions/latest",
            "projects/123/secrets/s/versions/0",
            "projects/123/secrets/s/versions/latest/extra",
            "projects/123/secrets/s/versions/latest\n",
            "projects/123/secrets/s",
            "",
            None,
        ]
        for name in bad_names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_secret_name(name)
                with self.assertRaises(ValueError):
                    Secrets.get_value(name)  # type: ignore[arg-type]

        mock_sm_client.assert_not_called()


class TestTerraformSecretExfiltrationHardening(unittest.TestCase):
    def setUp(self):
        self.tf_text = TF_API_CONNECTOR.read_text(encoding="utf-8")

    def test_runner_sa_does_not_have_project_wide_secret_accessor(self):
        runner_block = re.search(
            r'resource "google_project_iam_member" "load-0-api-fnc-runner-sa-iam"\s*\{[^}]*\}',
            self.tf_text,
            re.DOTALL,
        )
        self.assertIsNotNone(runner_block)
        self.assertNotIn(
            '"roles/secretmanager.secretAccessor"',
            runner_block.group(0),
            "load-0-api-fnc-runner-sa must not receive project-wide roles/secretmanager.secretAccessor.",
        )

    def test_load_function_configures_server_side_allowed_workflows(self):
        self.assertIn("ALLOWED_WORKFLOWS = jsonencode({", self.tf_text)
        self.assertIn("for wf in local.workflows", self.tf_text)
        self.assertIn('ingress_settings = "ALLOW_INTERNAL_ONLY"', self.tf_text)
        self.assertNotIn('"allUsers"', self.tf_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
