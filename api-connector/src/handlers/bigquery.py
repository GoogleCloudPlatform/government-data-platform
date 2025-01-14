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
from urllib.parse import urlencode

from google.cloud import tasks_v2

from ..config import config
from ..logger import logger
from ..utils import Utils


class BigQueryRoutineRequest:
    @staticmethod
    def execute(request: Any) -> Tuple[Any, int]:
        logger.debug("BigQuery Routine request received.")

        calls = Utils.get_property(request, "calls", required=True)
        replies = []

        for bq_args in calls:
            logger.debug(bq_args)

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
                for i, arg in enumerate(expected_args.keys()):
                    expected_args[arg] = bq_args[i]
                    if arg in payload.keys():
                        try:
                            payload[arg] = json.loads(bq_args[i])
                        except:
                            payload[arg] = bq_args[i]
            except KeyError as ke:
                log_info = {
                    "error": f"Unable to parse BigQuery Routine arguments. Are there missing parameters? {ke}"
                }
                logger.error(log_info)
                replies.append(log_info)
                continue

            # Fix query strings
            payload["query_string"] = urlencode(payload["query_string"])  # type: ignore

            # Get log table
            log_table = expected_args["result_table"].replace("tbl_result", "tbl_process_log")  # type: ignore

            try:
                taskClient = tasks_v2.CloudTasksClient()

                function_uri = f"https://{config.REGION}-{config.PROJECT_ID}.cloudfunctions.net/{config.FUNCTION_NAME}"

                task_descriptor = tasks_v2.Task(
                    http_request=tasks_v2.HttpRequest(
                        http_method=tasks_v2.HttpMethod.POST,
                        url=function_uri,
                        headers={"Content-type": "application/json"},
                        body=json.dumps(payload).encode() if payload else None,
                    )
                )

                logger.debug(
                    {"parent": expected_args["queue_name"], "task": task_descriptor}
                )

                task = taskClient.create_task(
                    request={
                        "parent": expected_args["queue_name"],
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
                logger.error(e)
                log_info = {"error": f"Error adding request to the queue: {e}"}

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
