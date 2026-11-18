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

import json
from datetime import datetime
from typing import Any, Tuple

import requests

from google.cloud import pubsub_v1

from ..auth import TokenAuth
from ..config import config
from ..gsecrets import Secrets
from ..logger import logger
from ..models.enums.auth_type import AuthType
from ..utils import Utils
from ..workflow_policy import (
    WorkflowAuthorizationError,
    WorkflowPolicy,
    WorkflowPolicyConfigError,
    validate_result_table,
)

_DEFAULT_TIMEOUT_SECONDS = 10
_MAX_TIMEOUT_SECONDS = 300


class CloudTaskRequest:
    @staticmethod
    def _sanitize_headers(headers: Any, has_auth: bool) -> Any:
        if headers is None:
            return None
        if not isinstance(headers, dict):
            raise ValueError("Invalid headers parameter.")
        blocked = {"host", "proxy-authorization"}
        if has_auth:
            blocked.add("authorization")
        return {
            k: v
            for k, v in headers.items()
            if isinstance(k, str) and k.lower() not in blocked
        }

    @staticmethod
    def api_call(
        uri: str,
        auth_type: AuthType,
        credentials: Any,
        query_string: str,
        payload: Any,
        timeout: int,
        headers: Any,
        method: str,
    ):
        logger.debug(f"{method.upper()} '{uri}'...")

        auth_payload = None

        match (auth_type):
            case AuthType.CLIENT_CREDENTIALS:
                auth_payload = TokenAuth(token=credentials, auth_scheme="access_token")
            case AuthType.HTTP_BASIC:
                auth_payload = (credentials[0], credentials[1])
            case AuthType.NONE:
                auth_payload = None

        request_matcher = {
            "get": requests.get,
            "post": requests.post,
            "put": requests.put,
            "delete": requests.delete,
        }

        if method.lower() in request_matcher.keys():
            return request_matcher[method.lower()](
                uri,
                allow_redirects=False,
                auth=auth_payload,
                data=payload,
                headers=headers,
                params=query_string,
                timeout=timeout,
            )
        else:
            msg = f"HTTP Method '{method}' is not supported."
            logger.error(msg)
            raise Exception(msg)

    @staticmethod
    def execute(request: Any) -> Tuple[Any, int]:
        logger.debug("Cloud Task request received.")

        workflow_id = Utils.get_property(request, "workflow_id")
        request_config = Utils.get_property(request, "request_config")
        auth = Utils.get_property(request, "auth")

        # Enforce server-side workflow policy before touching Secret Manager or HTTP.
        try:
            authorized = WorkflowPolicy.authorize(workflow_id, request_config, auth)
        except WorkflowPolicyConfigError as ce:
            logger.error(f"Workflow policy configuration error: {ce}")
            return "Server configuration error for workflow policy.", 503
        except WorkflowAuthorizationError as ae:
            logger.error(f"Rejected unauthorized Cloud Task request: {ae}")
            return "Unauthorized workflow request.", 403

        raw_result_table = Utils.get_property(request, "result_table")
        try:
            validated_result_table = validate_result_table(raw_result_table)
        except ValueError as ve:
            logger.error(f"Rejected Cloud Task request with invalid result_table: {ve}")
            return "Invalid result_table parameter.", 400

        raw_headers = Utils.get_property(request, "headers")
        try:
            headers = CloudTaskRequest._sanitize_headers(
                raw_headers,
                has_auth=authorized.auth_type != AuthType.NONE,
            )
        except ValueError as ve:
            logger.error(f"Rejected Cloud Task request with invalid headers: {ve}")
            return "Invalid headers parameter.", 400

        raw_timeout = request_config.get("timeout")
        if (
            isinstance(raw_timeout, (int, float))
            and not isinstance(raw_timeout, bool)
            and 0 < raw_timeout <= _MAX_TIMEOUT_SECONDS
        ):
            timeout = raw_timeout
        else:
            timeout = _DEFAULT_TIMEOUT_SECONDS

        logger.debug("Processing authentication type...")
        credentials = None
        if authorized.auth_type in (AuthType.CLIENT_CREDENTIALS, AuthType.HTTP_BASIC):
            try:
                secret_data = json.loads(Secrets.get_value(authorized.secret_name))
                logger.info(
                    f"Obtained secret authentication data for '{authorized.secret_name}'"
                )
            except Exception as e:
                logger.error(
                    f"Could not retrieve secret '{authorized.secret_name}': {e}"
                )
                return "Could not retrieve secret.", 500

            match (authorized.auth_type):
                case AuthType.CLIENT_CREDENTIALS:
                    logger.debug("Requesting credentials from server...")

                    try:
                        client_secret = Utils.get_property(
                            secret_data, "client_secret", required=True
                        )
                    except KeyError as ke:
                        logger.error(f"Secret missing required OAuth field: {ke}")
                        return "Invalid secret format.", 500

                    token_res = requests.post(
                        url=authorized.auth_server,
                        data={"grant_type": "client_credentials"},
                        allow_redirects=False,
                        auth=(authorized.client_id, client_secret),
                        timeout=timeout,
                    )

                    if token_res.status_code != 200:
                        logger.error(
                            "Failed to obtain token from OAuth 2.0 server: "
                            f"status={token_res.status_code}"
                        )
                        return "Failed to obtain token from the OAuth 2.0 server.", 500

                    access_token = Utils.get_property(
                        token_res.json(), "access_token", required=True
                    )
                    credentials = access_token

                    logger.debug("Success.")

                case AuthType.HTTP_BASIC:
                    logger.debug(
                        "Accessing secret properties (username, password)..."
                    )

                    try:
                        credentials = (
                            Utils.get_property(secret_data, "username", required=True),
                            Utils.get_property(secret_data, "password", required=True),
                        )
                    except KeyError as ke:
                        logger.error(f"Secret missing required HTTP Basic field: {ke}")
                        return "Invalid secret format.", 500

                    logger.debug("Success.")

            logger.info(
                "Credential processing complete. "
                f"Will use authentication type '{authorized.auth_type.name}'."
            )

        body = Utils.get_property(request, "body")
        query_string = Utils.get_property(request, "query_string")
        result_table = validated_result_table.replace("tbl_process_log", "tbl_result")
        log_table = result_table.replace("tbl_result", "tbl_process_log")

        rpl = result_table.replace("tbl_process_log", "*").replace("tbl_result", "*")
        logger.info(f"Results will be sent to '{rpl}'.")

        res = CloudTaskRequest.api_call(
            authorized.uri,
            authorized.auth_type,
            credentials,
            query_string,
            body,
            timeout,
            headers,
            authorized.method,
        )

        # Persist response on BigQuery
        Utils.save_bigquery(
            result_table,
            {
                "request": {
                    "uri": authorized.uri,
                    "method": authorized.method,
                    "auth_type": authorized.auth_type.name,
                    "query_string": query_string,
                    "body": json.dumps(body),
                },
                "request_time": f"{datetime.now().isoformat()}",
                "elapsed_time": res.elapsed.total_seconds(),
                "response": {
                    "status_code": res.status_code,
                    "headers": json.dumps(dict(res.headers)),
                    "body": res.text,
                },
            },
        )

        # Send response to Pub/Sub, if configured
        if config.PUBSUB_TOPICS:
            topics = json.loads(config.PUBSUB_TOPICS)
            topics = [
                [v for k, v in t.items() if k == authorized.workflow_id]
                for t in topics
            ]
            topic = next((x for xs in topics for x in xs), False)
            if topic:
                publisher = pubsub_v1.PublisherClient()

                logger.debug(
                    f"Publishing response data from '{authorized.workflow_id}' to '{topic}'..."
                )
                publisher.publish(str(topic), res.text.encode("utf-8"))
                logger.debug("Done.")

        # Log results/status
        if res.status_code == 200:
            log_info = {"status_code": res.status_code}
        else:
            log_info = {"status_code": res.status_code, "error_message": res.text}

        Utils.save_bigquery(
            log_table,
            {
                "query_string": query_string,
                "body": json.dumps(body),
                "result": json.dumps(log_info),
                "exec_time": f"{datetime.now().isoformat()}",
            },
        )

        ctype_header = res.headers.get("Content-Type")
        res_out = ""

        if ctype_header and ctype_header.startswith("application/json"):
            res_out = json.dumps(res.json())
        else:
            res_out = res.text

        logger.debug(res_out)
        return res_out, 202
