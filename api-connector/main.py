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

import traceback

import flask
import flask.typing
import functions_framework

from .src.handler import Handler
from .src.logger import logger


@functions_framework.http
def main(request: flask.Request) -> flask.typing.ResponseReturnValue:
    try:
        (msg, code) = Handler.execute(request)
        return flask.Response(msg + "\n"), code
    except Exception as e:
        logger.error(f"Exception occurred: {traceback.format_exception(e)}")
        return flask.Response(f"Exception occurred: {e}"), 500
