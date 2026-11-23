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

from typing import Any, Tuple

import flask

from .handlers.bigquery import BigQueryRoutineRequest
from .handlers.cloud_task import CloudTaskRequest
from .logger import logger
from .models.enums.request_source import Sources
from .utils import Utils


class Handler:
    @staticmethod
    def execute(request: flask.Request) -> Tuple[Any, int]:
        req_json = request.get_json(silent=True)
        if not isinstance(req_json, dict) or not req_json:
            return "Invalid request.", 400

        raw_source = Utils.get_property(req_json, "source")
        source = (
            Sources.BIGQUERY_ROUTINE.name if raw_source in (None, "") else raw_source
        )
        if not isinstance(source, str) or source not in Sources.__members__:
            logger.error("Rejected request with unsupported source type.")
            return "Unsupported request source.", 422

        match Sources[source]:
            case Sources.BIGQUERY_ROUTINE:
                (res, _) = BigQueryRoutineRequest.execute(req_json)
                return res, 200
            case Sources.CLOUD_TASK:
                (res, status_code) = CloudTaskRequest.execute(req_json)
                if status_code in (400, 403, 422, 503):
                    return res, status_code
                return res, 202  # This status code avoids retries by Cloud Tasks.
