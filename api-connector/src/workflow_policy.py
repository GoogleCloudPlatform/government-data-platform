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

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from .config import config
from .models.enums.auth_type import AuthType

_SECRET_VERSION_RE = re.compile(
    r"^projects/[A-Za-z0-9-]+/secrets/[A-Za-z0-9_-]+/versions/(?:latest|[1-9][0-9]*)$"
)
_RESULT_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_-]+$")
_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_DISALLOWED_HOSTS = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.goog",
        "metadata.google.internal",
    }
)


class WorkflowPolicyConfigError(ValueError):
    """Raised when server-side ALLOWED_WORKFLOWS policy is missing or invalid."""


class WorkflowAuthorizationError(ValueError):
    """Raised when an incoming request violates the server-side workflow policy."""


@dataclass(frozen=True)
class AuthorizedWorkflow:
    workflow_id: str
    uri: str
    method: str
    auth_type: AuthType
    secret_name: Optional[str] = None
    auth_server: Optional[str] = None
    client_id: Optional[str] = None


def validate_secret_name(secret_name: Any) -> str:
    if not isinstance(secret_name, str) or not _SECRET_VERSION_RE.fullmatch(
        secret_name
    ):
        raise ValueError("Invalid secret_name parameter.")
    return secret_name


def validate_result_table(result_table: Any) -> str:
    if not isinstance(result_table, str) or not _RESULT_TABLE_RE.fullmatch(
        result_table
    ):
        raise ValueError("Invalid result_table parameter.")
    return result_table


def _is_disallowed_ip(hostname: str) -> bool:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_https_url(url: Any, field_name: str = "url") -> str:
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise ValueError(f"Invalid {field_name}: must be a valid HTTPS URL.")

    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError(f"Invalid {field_name}: must be a valid HTTPS URL.")

    hostname = parsed.hostname.rstrip(".").lower()
    if (
        hostname in _DISALLOWED_HOSTS
        or hostname.endswith(".localhost")
        or _is_disallowed_ip(hostname)
    ):
        raise ValueError(f"Invalid {field_name}: host is not allowed.")

    return url


class WorkflowPolicy:
    @staticmethod
    def _load_policies() -> dict:
        raw = config.ALLOWED_WORKFLOWS
        if not isinstance(raw, str) or not raw.strip():
            raise WorkflowPolicyConfigError("ALLOWED_WORKFLOWS must be configured.")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as err:
            raise WorkflowPolicyConfigError(
                "ALLOWED_WORKFLOWS must be valid JSON."
            ) from err
        if not isinstance(parsed, dict) or not parsed:
            raise WorkflowPolicyConfigError(
                "ALLOWED_WORKFLOWS must be a non-empty JSON object."
            )
        return parsed

    @staticmethod
    def _parse_policy_entry(workflow_id: str, entry: Any) -> AuthorizedWorkflow:
        if not isinstance(entry, dict):
            raise WorkflowPolicyConfigError(
                f"Workflow policy for '{workflow_id}' must be an object."
            )

        try:
            allowed_uri = validate_https_url(entry.get("uri"), "policy.uri")
        except ValueError as err:
            raise WorkflowPolicyConfigError(str(err)) from err

        raw_method = entry.get("method") or "POST"
        if not isinstance(raw_method, str) or raw_method.upper() not in _SUPPORTED_METHODS:
            raise WorkflowPolicyConfigError(
                f"Workflow policy for '{workflow_id}' has unsupported HTTP method."
            )
        allowed_method = raw_method.upper()

        raw_auth_type = entry.get("auth_type") or AuthType.NONE.name
        if not isinstance(raw_auth_type, str) or raw_auth_type not in AuthType.__members__:
            raise WorkflowPolicyConfigError(
                f"Workflow policy for '{workflow_id}' has unsupported auth_type."
            )
        allowed_auth_type = AuthType[raw_auth_type]

        allowed_secret: Optional[str] = None
        allowed_auth_server: Optional[str] = None
        allowed_client_id: Optional[str] = None

        if allowed_auth_type in (AuthType.HTTP_BASIC, AuthType.CLIENT_CREDENTIALS):
            try:
                allowed_secret = validate_secret_name(entry.get("secret_name"))
            except ValueError as err:
                raise WorkflowPolicyConfigError(
                    f"Workflow policy for '{workflow_id}' has invalid secret_name."
                ) from err

        if allowed_auth_type == AuthType.CLIENT_CREDENTIALS:
            try:
                allowed_auth_server = validate_https_url(
                    entry.get("auth_server"), "policy.auth_server"
                )
            except ValueError as err:
                raise WorkflowPolicyConfigError(str(err)) from err

            client_id = entry.get("client_id")
            if not isinstance(client_id, str) or not client_id.strip():
                raise WorkflowPolicyConfigError(
                    f"Workflow policy for '{workflow_id}' requires client_id."
                )
            allowed_client_id = client_id

        return AuthorizedWorkflow(
            workflow_id=workflow_id,
            uri=allowed_uri,
            method=allowed_method,
            auth_type=allowed_auth_type,
            secret_name=allowed_secret,
            auth_server=allowed_auth_server,
            client_id=allowed_client_id,
        )

    @staticmethod
    def authorize(
        workflow_id: Any,
        request_config: Any,
        auth: Any,
    ) -> AuthorizedWorkflow:
        policies = WorkflowPolicy._load_policies()

        if not isinstance(workflow_id, str) or not workflow_id or workflow_id not in policies:
            raise WorkflowAuthorizationError("Workflow is not authorized.")

        authorized = WorkflowPolicy._parse_policy_entry(
            workflow_id, policies[workflow_id]
        )

        if not isinstance(request_config, dict):
            raise WorkflowAuthorizationError("Invalid request_config.")

        req_uri = request_config.get("uri")
        if not isinstance(req_uri, str) or req_uri != authorized.uri:
            raise WorkflowAuthorizationError(
                "Request URI is not authorized for workflow."
            )

        req_method = request_config.get("method") or "POST"
        if not isinstance(req_method, str) or req_method.upper() != authorized.method:
            raise WorkflowAuthorizationError(
                "Request HTTP method is not authorized for workflow."
            )

        if authorized.auth_type == AuthType.NONE:
            if auth is not None:
                if not isinstance(auth, dict):
                    raise WorkflowAuthorizationError("Invalid auth parameter.")
                req_auth_type = auth.get("type")
                if req_auth_type is not None and req_auth_type != AuthType.NONE.name:
                    raise WorkflowAuthorizationError(
                        "Authentication type is not authorized for workflow."
                    )
                if auth.get("secret_name"):
                    raise WorkflowAuthorizationError(
                        "Secret access is not authorized for workflow."
                    )
        else:
            if not isinstance(auth, dict):
                raise WorkflowAuthorizationError(
                    "Authentication configuration is required for workflow."
                )

            req_auth_type = auth.get("type")
            if req_auth_type != authorized.auth_type.name:
                raise WorkflowAuthorizationError(
                    "Authentication type is not authorized for workflow."
                )

            req_secret_name = auth.get("secret_name")
            try:
                validated_secret = validate_secret_name(req_secret_name)
            except ValueError as err:
                raise WorkflowAuthorizationError(
                    "Secret name is not authorized for workflow."
                ) from err
            if validated_secret != authorized.secret_name:
                raise WorkflowAuthorizationError(
                    "Secret name is not authorized for workflow."
                )

            if authorized.auth_type == AuthType.CLIENT_CREDENTIALS:
                req_auth_server = auth.get("auth_server")
                if (
                    not isinstance(req_auth_server, str)
                    or req_auth_server != authorized.auth_server
                ):
                    raise WorkflowAuthorizationError(
                        "OAuth auth_server is not authorized for workflow."
                    )
                req_client_id = auth.get("client_id")
                if (
                    not isinstance(req_client_id, str)
                    or req_client_id != authorized.client_id
                ):
                    raise WorkflowAuthorizationError(
                        "OAuth client_id is not authorized for workflow."
                    )

        return authorized
