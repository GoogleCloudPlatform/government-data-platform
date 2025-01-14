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
from ..gsecrets import Secrets
from ..logger import logger
from ..models.enums.auth_type import AuthType
from ..utils import Utils
from ..config import config


class CloudTaskRequest:
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

        request_matcher = {
            "get": requests.get,
            "post": requests.post,
            "put": requests.put,
            "delete": requests.delete,
        }

        if method.lower() in request_matcher.keys():
            return request_matcher[method.lower()](
                uri,
                allow_redirects=True,
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

        logger.debug("Processing authentication type...")
        auth = Utils.get_property(request, "auth")
        credentials = None
        auth_type = ""
        secret_data = None
        if auth:
            try:
                secret_name = Utils.get_property(auth, "secret_name", required=True)
                secret_data = json.loads(Secrets.get_value(secret_name))
                logger.info(f"Obtained secret authentication data for '{secret_name}'")
            except Exception as e:
                return f"Could not retrieve secret: {e}", 500

            try:
                auth_type = Utils.get_property(auth, "type", required=True)

                match (AuthType[auth_type]):
                    case AuthType.CLIENT_CREDENTIALS:
                        logger.debug("Requesting credentials from server...")

                        auth_server = Utils.get_property(
                            auth, "auth_server", required=True
                        )
                        client_id = Utils.get_property(auth, "client_id", required=True)
                        client_secret = Utils.get_property(
                            secret_data, "client_secret", required=True
                        )

                        token_res = requests.post(
                            url=auth_server,
                            data={"grant_type": "client_credentials"},
                            allow_redirects=False,
                            auth=(client_id, client_secret),
                        )

                        if token_res.status_code != 200:
                            msg = f"Failed to obtain token from the OAuth 2.0 server: {token_res.text}"
                            logger.error(msg)
                            return msg, 500

                        access_token = Utils.get_property(
                            token_res.json(), "access_token", required=True
                        )
                        credentials = access_token

                        logger.debug("Success.")

                    case AuthType.HTTP_BASIC:
                        logger.debug(
                            "Accessing secret properties (username, password)..."
                        )

                        credentials = (
                            Utils.get_property(secret_data, "username", required=True),
                            Utils.get_property(secret_data, "password", required=True),
                        )

                        logger.debug("Success.")

            except KeyError as ke:
                msg = f"Authentication type of '{auth_type}' is not supported."
                logger.error(msg)
                return msg, 422

            logger.info(
                f"Credential processing complete. Will use authentication type '{auth_type}'."
            )

        request_config = Utils.get_property(request, "request_config", required=True)

        uri = Utils.get_property(request_config, "uri", required=True)
        method = Utils.get_property(request_config, "method") or "POST"
        timeout = Utils.get_property(request_config, "timeout") or 10

        body = Utils.get_property(request, "body")
        headers = Utils.get_property(request, "headers")
        query_string = Utils.get_property(request, "query_string")
        result_table = Utils.get_property(request, "result_table").replace(
            "tbl_process_log", "tbl_result"
        )
        log_table = result_table.replace("tbl_result", "tbl_process_log")

        rpl = result_table.replace("tbl_process_log", "*").replace("tbl_result", "*")
        logger.info(f"Results will be sent to '{rpl}'.")

        res = CloudTaskRequest.api_call(
            uri,
            AuthType[auth_type],
            credentials,
            query_string,
            body,
            timeout,
            headers,
            method,
        )

        # Persist response on BigQuery
        Utils.save_bigquery(
            result_table,
            {
                "request": {
                    "uri": uri,
                    "method": method,
                    "auth_type": auth_type,
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
            topics = [[v for k, v in t.items() if k == workflow_id] for t in topics]
            topic = next((x for xs in topics for x in xs), False)
            if topic:
                publisher = pubsub_v1.PublisherClient()

                logger.debug(
                    f"Publishing response data from '{workflow_id}' to '{topic}'..."
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

        # logger.debug(f"Got response: {res_json}")
        ctype_header = res.headers.get("Content-Type")
        res_out = ""

        if ctype_header and ctype_header.startswith("application/json"):
            res_out = json.dumps(res.json())
        else:
            res_out = res.text

        logger.debug(res_out)
        return res_out, 202
