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

import os


class config:
    @staticmethod
    def __env(key: str, required=True):
        val = os.getenv(key)
        if not val and required:
            raise KeyError(f"Environment variable '{key}' must be set.")
        return val

    PROJECT_ID = __env("PROJECT_ID", required=True)
    PROJECT_NUMBER = __env("PROJECT_NUMBER", required=False)
    FUNCTION_NAME = __env("FUNCTION_NAME", required=False)
    FUNCTION_URI = __env("FUNCTION_URI", required=False)
    REGION = __env("REGION", required=False)
    TASK_INVOKER_SA = __env("TASK_INVOKER_SA", required=False)
    ALLOWED_QUEUE_PREFIX = (
        __env("ALLOWED_QUEUE_PREFIX", required=False) or "cloud-task-api-"
    )
    ALLOWED_WORKFLOWS = __env("ALLOWED_WORKFLOWS", required=False)
    ENVIRONMENT = __env("ENVIRONMENT", required=False) or "local"
    PUBSUB_TOPICS = __env("PUBSUB_TOPICS", required=False)
