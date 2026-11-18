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
import re
from datetime import datetime
from typing import Any, Tuple
from urllib.parse import urlencode, urlparse

from google.cloud import tasks_v2

from ..config import config
from ..logger import logger
from ..utils import Utils

_QUEUE_ID_MAX_LEN = 100
_QUEUE_PREFIX_RE = re.compile(r"^[A-Za-z0-9-]+$")


class BigQueryRoutineRequest:
    @staticmethod
    def _validate_queue_name(queue_name: Any) -> str:
        project_id = config.PROJECT_ID
        region = config.REGION
        allowed_prefix = config.ALLOWED_QUEUE_PREFIX

        if not isinstance(project_id, str) or not project_id:
            raise ValueError("PROJECT_ID must be configured.")
        if not isinstance(region, str) or not region:
            raise ValueError("REGION must be configured.")
        if (
            not isinstance(allowed_prefix, str)
            or not allowed_prefix
            or not _QUEUE_PREFIX_RE.fullmatch(allowed_prefix)
        ):
            raise ValueError("ALLOWED_QUEUE_PREFIX is misconfigured.")

        if not isinstance(queue_name, str):
            raise ValueError("Invalid queue_name parameter.")

        pattern = re.compile(
            rf"projects/{re.escape(project_id)}/locations/{re.escape(region)}/queues/"
            rf"({re.escape(allowed_prefix)}[A-Za-z0-9-]+)"
        )
        match = pattern.fullmatch(queue_name)
        if not match:
            raise ValueError("Invalid queue_name parameter.")

        queue_id = match.group(1)
        if len(queue_id) > _QUEUE_ID_MAX_LEN:
            raise ValueError("Invalid queue_name parameter.")

        # Reconstruct canonical resource path from server-side config and validated ID.
        return f"projects/{project_id}/locations/{region}/queues/{queue_id}"

    @staticmethod
    def _build_function_uri() -> str:
        if config.FUNCTION_URI:
            if not isinstance(config.FUNCTION_URI, str):
                raise ValueError("FUNCTION_URI must be a valid HTTPS URL.")
            uri = config.FUNCTION_URI.rstrip("/")
            parsed = urlparse(uri)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or any(ch.isspace() for ch in uri)
            ):
                raise ValueError("FUNCTION_URI must be a valid HTTPS URL.")
            return uri

        if not config.PROJECT_ID or not config.REGION or not config.FUNCTION_NAME:
            raise ValueError(
                "PROJECT_ID, REGION, and FUNCTION_NAME must be configured."
            )

        if config.PROJECT_NUMBER:
            return (
                f"https://{config.FUNCTION_NAME}-{config.PROJECT_NUMBER}"
                f".{config.REGION}.run.app"
            )

        return (
            f"https://{config.REGION}-{config.PROJECT_ID}"
            f".cloudfunctions.net/{config.FUNCTION_NAME}"
        )

    @staticmethod
    def _get_task_invoker_sa() -> str:
        invoker_sa = config.TASK_INVOKER_SA
        if (
            not isinstance(invoker_sa, str)
            or not invoker_sa
            or invoker_sa.strip() != invoker_sa
            or "@" not in invoker_sa
        ):
            raise ValueError(
                "TASK_INVOKER_SA must be configured to enqueue authenticated Cloud Tasks."
            )
        return invoker_sa

    @staticmethod
    def execute(request: Any) -> Tuple[Any, int]:
        logger.debug("BigQuery Routine request received.")

        calls = Utils.get_property(request, "calls", required=True)
        if not isinstance(calls, list):
            raise ValueError("Invalid 'calls' payload.")

        replies = []

        for bq_args in calls:
            # The following arguments are received from the BigQuery Routine, in order:
            expected_args = {
                "workflow_id": None,
                "request_config": None,
                "auth": None,
                "headers": None,
                "query_string": None,
                "body": None,
                "result_table": None,
                "queue_name": None,
            }

            # This is what the CLOUD_TASK routine expects
            payload = {
                "workflow_id": None,
                "request_config": None,
                "headers": None,
                "query_string": None,
                "auth": None,
                "body": None,
                "result_table": None,
                "source": "CLOUD_TASK",
            }

            try:
                if not isinstance(bq_args, (list, tuple)) or len(bq_args) != len(
                    expected_args
                ):
                    raise IndexError("Expected 8 positional BigQuery Routine arguments.")

                for i, arg in enumerate(expected_args.keys()):
                    expected_args[arg] = bq_args[i]
                    if arg in payload.keys():
                        try:
                            payload[arg] = json.loads(bq_args[i])
                        except (json.JSONDecodeError, TypeError):
                            payload[arg] = bq_args[i]
            except (IndexError, KeyError, TypeError) as err:
                logger.error(f"Unable to parse BigQuery Routine arguments: {err}")
                replies.append(
                    {"error": "Unable to parse BigQuery Routine arguments."}
                )
                continue

            # Strictly validate queue_name before touching Cloud Tasks or BigQuery.
            try:
                validated_queue_path = BigQueryRoutineRequest._validate_queue_name(
                    expected_args["queue_name"]
                )
            except ValueError as ve:
                logger.error(f"Rejected Cloud Tasks queue_name: {ve}")
                replies.append({"error": "Invalid queue_name parameter."})
                continue

            if not isinstance(expected_args["result_table"], str) or not expected_args[
                "result_table"
            ]:
                logger.error("Rejected BigQuery Routine call: missing result_table.")
                replies.append({"error": "Invalid result_table parameter."})
                continue

            # Validate target URI and OIDC service account before enqueuing (fail closed).
            try:
                function_uri = BigQueryRoutineRequest._build_function_uri()
                invoker_sa = BigQueryRoutineRequest._get_task_invoker_sa()
            except ValueError as ve:
                logger.error(f"Cloud Tasks dispatch configuration error: {ve}")
                replies.append(
                    {"error": "Server configuration error for task dispatch."}
                )
                continue

            # Fix query strings
            if isinstance(payload["query_string"], dict):
                payload["query_string"] = urlencode(payload["query_string"])
            elif payload["query_string"] is None:
                payload["query_string"] = ""

            # Get log table
            log_table = expected_args["result_table"].replace(
                "tbl_result", "tbl_process_log"
            )

            try:
                taskClient = tasks_v2.CloudTasksClient()

                task_descriptor = tasks_v2.Task(
                    http_request=tasks_v2.HttpRequest(
                        http_method=tasks_v2.HttpMethod.POST,
                        url=function_uri,
                        headers={"Content-type": "application/json"},
                        body=json.dumps(payload).encode() if payload else None,
                        oidc_token=tasks_v2.OidcToken(
                            service_account_email=invoker_sa,
                            audience=function_uri,
                        ),
                    )
                )

                logger.debug(f"Enqueuing task to '{validated_queue_path}'.")

                task = taskClient.create_task(
                    request={
                        "parent": validated_queue_path,
                        "task": task_descriptor,
                    }
                )
                logger.info(f"Created task: {task.name}")

                log_info = {"response": "Request added to the queue."}
                Utils.save_bigquery(
                    log_table,
                    {
                        "query_string": expected_args["query_string"],
                        "headers": expected_args["headers"],
                        "body": expected_args["body"],
                        "result": json.dumps(log_info),
                        "exec_time": f"{datetime.now().isoformat()}",
                    },
                )
                replies.append(log_info)

            except Exception as e:
                logger.error(f"Error adding request to the queue: {e}")
                log_info = {"error": "Error adding request to the queue."}

                Utils.save_bigquery(
                    log_table,
                    {
                        "query_string": expected_args["query_string"],
                        "headers": expected_args["headers"],
                        "body": expected_args["body"],
                        "result": json.dumps(log_info),
                        "exec_time": f"{datetime.now().isoformat()}",
                    },
                )

                replies.append(log_info)
        return json.dumps({"replies": replies}), 200
