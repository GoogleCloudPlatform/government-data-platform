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

from typing import Any

from google.cloud import bigquery

from .config import config
from .logger import logger


class Utils:
    @staticmethod
    def get_property(obj: Any, key: str, required=False):
        val = obj.get(key)
        if not val and required:
            raise KeyError(f"Missing property: '{key}'")
        return val

    @staticmethod
    def save_bigquery(dataset_and_table, data, project_id=config.PROJECT_ID):
        logger.debug(f"Attempting to write to table '{dataset_and_table}'")

        client = bigquery.Client()
        table = client.get_table(f"{project_id}.{dataset_and_table}")
        rows = [data]

        errors = client.insert_rows_json(table, rows)

        if errors:
            logger.error(f"Errors: {errors}")
            return False

        logger.debug("Success.")

        return True
