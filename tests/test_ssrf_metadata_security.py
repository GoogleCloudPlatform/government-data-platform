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

"""Regression guards for CWE-918 unauthenticated full-read SSRF and GCP
metadata token theft via request_config.uri, attacker-controlled headers,
obfuscated/DNS-resolved internal IPs, direct api_call sink usage, and
upstream response body reflection in api-connector.
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


_install_optional_stubs()
if str(API_CONNECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(API_CONNECTOR_DIR))

from src.config import config  # noqa: E402
from src.handler import Handler  # noqa: E402
from src.handlers.cloud_task import CloudTaskRequest  # noqa: E402
from src.models.enums.auth_type import AuthType  # noqa: E402
from src.workflow_policy import (  # noqa: E402
    sanitize_headers,
    validate_https_url,
    validate_resolved_https_url,
)

PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
METADATA_ADDRINFO = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
]


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


class TestSsrfUrlAndDnsGuardsCWE918(unittest.TestCase):
    def test_validate_https_url_blocks_obfuscated_ips_idna_and_internal_suffixes(self):
        bypass_urls = [
            # GCP metadata server direct & subdomains
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            "https://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            "https://sub.metadata.google.internal/computeMetadata/v1/",
            "https://vm.c.myproj.internal/secret",
            "https://foo.metadata.goog/",
            "https://internal-service.local/api",
            "https://internal-service.localdomain/api",
            "https://singlelabel/api",
            # Obfuscated IPv4 encodings of 169.254.169.254 / 127.0.0.1 / 0.0.0.0
            "https://2852039166/computeMetadata/v1/",
            "https://0xa9fea9fe/computeMetadata/v1/",
            "https://0251.0376.0251.0376/computeMetadata/v1/",
            "https://0xa9.0xfe.0xa9.0xfe/computeMetadata/v1/",
            "https://169.254.43518/computeMetadata/v1/",
            "https://127.1/",
            "https://0177.0.0.1/",
            "https://0/",
            # Cloud metadata & CGNAT ranges
            "https://100.100.100.200/latest/meta-data/",
            "https://100.64.0.1/",
            # IPv6 metadata / NAT64 / 6to4 / zone IDs
            "https://[fd00:ec2::254]/latest/meta-data/",
            "https://[64:ff9b::169.254.169.254]/computeMetadata/v1/",
            "https://[64:ff9b:1::10.0.0.1]/",
            "https://[2002:a9fe:a9fe::1]/",
            "https://[fe80::1%25eth0]/",
            # Unicode / IDNA homoglyphs & fullwidth dots
            "https://169\u3002254\u3002169\u3002254/computeMetadata/v1/",
            "https://metadata\u3002google\u3002internal/computeMetadata/v1/",
            "https://\uff11\uff12\uff17.\uff10.\uff10.\uff11/",
            # Backslash & malformed port tricks
            "https://example.com\\@169.254.169.254/",
            "https://example.com:99999/",
            "https://example.com:abc/",
        ]
        for url in bypass_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_https_url(url)

    def test_validate_resolved_https_url_blocks_dns_to_metadata_private_or_unresolvable(
        self,
    ):
        disallowed_resolutions = [
            # Direct GCP metadata IP via DNS (e.g. nip.io or rebound record)
            METADATA_ADDRINFO,
            # Dual-record DNS mixing a public IP and an internal/metadata IP
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
            ],
            # RFC1918 internal IP
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.10.0.5", 443))],
            # Loopback IPv6
            [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))],
            # IPv4-mapped metadata IPv6
            [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("::ffff:169.254.169.254", 443, 0, 0),
                )
            ],
            # NAT64-wrapped metadata IPv6
            [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("64:ff9b::169.254.169.254", 443, 0, 0),
                )
            ],
            # Empty resolution
            [],
        ]
        for resolved in disallowed_resolutions:
            with self.subTest(resolved=resolved):
                with patch(
                    "src.workflow_policy.socket.getaddrinfo", return_value=resolved
                ):
                    with self.assertRaises(ValueError):
                        validate_resolved_https_url("https://api.example.com/v1/data")

        # Unresolvable DNS must fail closed.
        with patch(
            "src.workflow_policy.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            with self.assertRaises(ValueError):
                validate_resolved_https_url("https://api.example.com/v1/data")

        # Globally routable public IP passes.
        with patch(
            "src.workflow_policy.socket.getaddrinfo", return_value=PUBLIC_ADDRINFO
        ):
            self.assertEqual(
                validate_resolved_https_url("https://api.example.com/v1/data"),
                "https://api.example.com/v1/data",
            )


class TestMetadataHeaderSanitizationCWE918(unittest.TestCase):
    def test_strips_cloud_metadata_and_hop_by_hop_headers(self):
        raw_headers = {
            "Content-Type": "application/json",
            "X-API-Key": "ok-value",
            "Metadata-Flavor": "Google",
            "metadata-flavor": "Google",
            "X-Google-Metadata-Request": "True",
            "Metadata": "true",
            "X-Aws-Ec2-Metadata-Token": "tok",
            "X-Aws-Ec2-Metadata-Token-Ttl-Seconds": "21600",
            "Host": "metadata.google.internal",
            "Proxy-Authorization": "Basic abc",
            "Proxy-Connection": "keep-alive",
            "X-Forwarded-For": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
            "Transfer-Encoding": "chunked",
        }
        sanitized = sanitize_headers(raw_headers, has_auth=False)
        self.assertEqual(
            sanitized, {"Content-Type": "application/json", "X-API-Key": "ok-value"}
        )

    def test_rejects_crlf_injection_and_malformed_headers(self):
        malicious_headers = [
            {"X-Custom": "ok\r\nMetadata-Flavor: Google"},
            {"X-Custom": "ok\nMetadata-Flavor: Google"},
            {"X-Custom": "ok\x00bad"},
            {"Bad Header": "value"},
            {"Bad:Header": "value"},
            {"X-Num": 123},
            "not-a-dict",
        ]
        for hdrs in malicious_headers:
            with self.subTest(headers=hdrs):
                with self.assertRaises(ValueError):
                    sanitize_headers(hdrs, has_auth=False)


class TestCloudTaskFullReadSsrfCWE918(unittest.TestCase):
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
            patch.object(config, "ALLOWED_WORKFLOWS", ALLOWED_WORKFLOWS_FIXTURE),
            patch.object(config, "PUBSUB_TOPICS", None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    def test_blocks_reported_gcp_metadata_token_theft_exploit(
        self, mock_requests_get, mock_save_bq
    ):
        """Smallest reproduction of reported CWE-918 exploit: CLOUD_TASK targeting
        GCP metadata server with Metadata-Flavor: Google header.
        """
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "request_config": {
                    "uri": "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                    "method": "GET",
                },
                "headers": {"Metadata-Flavor": "Google"},
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 403)
        self.assertEqual(body, "Unauthorized workflow request.")
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.workflow_policy.socket.getaddrinfo", return_value=METADATA_ADDRINFO)
    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    def test_blocks_allowed_workflow_when_dns_resolves_to_metadata_ip(
        self, mock_requests_get, mock_save_bq, _mock_dns
    ):
        """Even if an allowlisted hostname is DNS-rebound to 169.254.169.254,
        execution must fail closed before making any HTTP request.
        """
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "request_config": {
                    "uri": "https://api.example.com/v1/data",
                    "method": "GET",
                },
                "headers": {"Metadata-Flavor": "Google"},
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        body, status = Handler.execute(req)
        self.assertEqual(status, 403)
        self.assertEqual(body, "Unauthorized workflow request.")
        mock_requests_get.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.post")
    @patch("src.handlers.cloud_task.Secrets.get_value")
    def test_blocks_oauth_auth_server_dns_to_metadata_before_touching_secrets(
        self, mock_get_secret, mock_requests_post, mock_save_bq
    ):
        def _dns_side_effect(host, *args, **kwargs):
            if host == "auth.partner.example":
                return METADATA_ADDRINFO
            return PUBLIC_ADDRINFO

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
        with patch(
            "src.workflow_policy.socket.getaddrinfo", side_effect=_dns_side_effect
        ), patch("src.handlers.cloud_task.logger") as mock_logger:
            body, status = Handler.execute(req)
        self.assertEqual(status, 403)
        self.assertEqual(body, "Unauthorized workflow request.")
        self.assertTrue(
            any(
                "auth.auth_server" in str(call.args[0])
                for call in mock_logger.error.call_args_list
            )
        )
        mock_get_secret.assert_not_called()
        mock_requests_post.assert_not_called()
        mock_save_bq.assert_not_called()

    @patch("src.workflow_policy.socket.getaddrinfo", return_value=PUBLIC_ADDRINFO)
    @patch("src.handlers.cloud_task.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.cloud_task.requests.get")
    def test_eliminates_full_read_response_reflection_and_strips_metadata_headers(
        self, mock_requests_get, mock_save_bq, _mock_dns
    ):
        """Authorized requests must strip Metadata-Flavor headers and must NOT
        reflect upstream response bodies back to the HTTP caller or logs
        (eliminating the full-read SSRF oracle while still persisting to BigQuery).
        """
        sensitive_upstream = '{"access_token": "ya29.SENSITIVE_TOKEN", "expires_in": 3599}'
        mock_requests_get.return_value = _make_mock_http_response(
            200, sensitive_upstream
        )
        req = _FakeFlaskRequest(
            {
                "source": "CLOUD_TASK",
                "workflow_id": "workflow1",
                "request_config": {
                    "uri": "https://api.example.com/v1/data",
                    "method": "GET",
                },
                "headers": {
                    "Content-Type": "application/json",
                    "Metadata-Flavor": "Google",
                    "X-Google-Metadata-Request": "True",
                },
                "result_table": "gdp_test_dwh_load_bq_0.tbl_result_20250101000000",
            }
        )
        with patch("src.handlers.cloud_task.logger") as mock_logger:
            body, status = Handler.execute(req)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"status": "processed", "status_code": 200})
        self.assertNotIn("ya29.SENSITIVE_TOKEN", body)
        for call in (
            mock_logger.debug.call_args_list
            + mock_logger.info.call_args_list
            + mock_logger.error.call_args_list
        ):
            self.assertNotIn("ya29.SENSITIVE_TOKEN", str(call))

        # Metadata headers must never reach requests.get.
        mock_requests_get.assert_called_once()
        sent_headers = mock_requests_get.call_args.kwargs["headers"]
        self.assertEqual(sent_headers, {"Content-Type": "application/json"})

        # Upstream response is still persisted to BigQuery tbl_result.
        self.assertEqual(mock_save_bq.call_count, 2)
        bq_result_row = mock_save_bq.call_args_list[0].args[1]
        self.assertEqual(bq_result_row["response"]["body"], sensitive_upstream)

    @patch("src.handlers.cloud_task.requests.get")
    def test_direct_api_call_sink_enforces_url_dns_and_header_guards(
        self, mock_requests_get
    ):
        """Defense-in-depth at CloudTaskRequest.api_call() itself."""
        with self.assertRaises(ValueError):
            CloudTaskRequest.api_call(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                AuthType.NONE,
                None,
                "",
                None,
                10,
                {"Metadata-Flavor": "Google"},
                "GET",
            )

        with self.assertRaises(ValueError):
            CloudTaskRequest.api_call(
                "https://2852039166/computeMetadata/v1/",
                AuthType.NONE,
                None,
                "",
                None,
                10,
                {"Metadata-Flavor": "Google"},
                "GET",
            )

        with patch(
            "src.workflow_policy.socket.getaddrinfo", return_value=METADATA_ADDRINFO
        ):
            with self.assertRaises(ValueError):
                CloudTaskRequest.api_call(
                    "https://api.example.com/v1/data",
                    AuthType.NONE,
                    None,
                    "",
                    None,
                    10,
                    {"Metadata-Flavor": "Google"},
                    "GET",
                )

        mock_requests_get.assert_not_called()

        # Authorized direct sink call strips Metadata-Flavor and disables redirects.
        mock_requests_get.return_value = _make_mock_http_response(200, '{"ok": true}')
        with patch(
            "src.workflow_policy.socket.getaddrinfo", return_value=PUBLIC_ADDRINFO
        ):
            CloudTaskRequest.api_call(
                "https://api.example.com/v1/data",
                AuthType.NONE,
                None,
                "",
                None,
                10,
                {"Metadata-Flavor": "Google", "Accept": "application/json"},
                "GET",
            )
        mock_requests_get.assert_called_once()
        self.assertEqual(
            mock_requests_get.call_args.kwargs["headers"],
            {"Accept": "application/json"},
        )
        self.assertFalse(mock_requests_get.call_args.kwargs["allow_redirects"])

    @patch("src.handlers.bigquery.Utils.save_bigquery", return_value=True)
    @patch("src.handlers.bigquery.tasks_v2.CloudTasksClient")
    def test_bq_routine_strips_metadata_headers_and_rejects_crlf_headers(
        self, mock_client_cls, mock_save_bq
    ):
        mock_client = MagicMock()
        mock_client.create_task.return_value = MagicMock(name="task-1")
        mock_client_cls.return_value = mock_client

        valid_queue = (
            "projects/gdp-test-lod/locations/us-west1/queues/cloud-task-api-20250101000000"
        )
        valid_table = "gdp_test_dwh_load_bq_0.tbl_process_log_20250101000000"
        req = _FakeFlaskRequest(
            {
                "calls": [
                    # CRLF header injection -> rejected before enqueuing
                    [
                        "workflow1",
                        json.dumps({"uri": "https://api.example.com/v1/data", "method": "GET"}),
                        json.dumps({"type": "NONE"}),
                        json.dumps({"X-Bad": "val\r\nMetadata-Flavor: Google"}),
                        "{}",
                        "{}",
                        valid_table,
                        valid_queue,
                    ],
                    # Metadata-Flavor header -> stripped from enqueued Cloud Task payload
                    [
                        "workflow1",
                        json.dumps({"uri": "https://api.example.com/v1/data", "method": "GET"}),
                        json.dumps({"type": "NONE"}),
                        json.dumps(
                            {
                                "Accept": "application/json",
                                "Metadata-Flavor": "Google",
                            }
                        ),
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
        self.assertEqual(replies[0], {"error": "Invalid headers parameter."})
        self.assertEqual(replies[1], {"response": "Request added to the queue."})

        mock_client.create_task.assert_called_once()
        enqueued_body = json.loads(
            mock_client.create_task.call_args.kwargs["request"][
                "task"
            ].http_request.body.decode("utf-8")
        )
        self.assertEqual(enqueued_body["headers"], {"Accept": "application/json"})


class TestTerraformSsrfBoundaryHardeningCWE918(unittest.TestCase):
    def setUp(self):
        self.tf_text = TF_API_CONNECTOR.read_text(encoding="utf-8")

    def test_load_function_blocks_unauthenticated_access_and_overbroad_runner_roles(
        self,
    ):
        self.assertIn('ingress_settings = "ALLOW_INTERNAL_ONLY"', self.tf_text)
        self.assertNotIn('"ALLOW_ALL"', self.tf_text)
        self.assertNotIn('"allUsers"', self.tf_text)
        self.assertNotIn('"allAuthenticatedUsers"', self.tf_text)
        self.assertIn("ALLOWED_WORKFLOWS = jsonencode({", self.tf_text)

        runner_block = re.search(
            r'resource "google_project_iam_member" "load-0-api-fnc-runner-sa-iam"\s*\{[^}]*\}',
            self.tf_text,
            re.DOTALL,
        )
        self.assertIsNotNone(runner_block)
        self.assertNotIn('"roles/secretmanager.secretAccessor"', runner_block.group(0))
        self.assertNotIn('"roles/iam.serviceAccountUser"', runner_block.group(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
