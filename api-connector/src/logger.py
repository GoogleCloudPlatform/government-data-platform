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

import logging

import coloredlogs

from .config import config

logger = logging.getLogger("api-connector")
style = {
    "critical": {"bold": True, "color": "red"},
    "debug": {},
    "error": {"color": "red"},
    "info": {},
    "notice": {"color": "magenta"},
    "spam": {"color": "green", "faint": True},
    "success": {"bold": True, "color": "green"},
    "verbose": {"color": "blue"},
    "warning": {"color": "yellow"},
}

if config.ENVIRONMENT == "local":
    format = "%(asctime)s %(levelname)-8s %(filename)s:%(lineno)s %(funcName)s -> %(message)s"
    coloredlogs.install(level="DEBUG", logger=logger, fmt=format, level_styles=style)  # type: ignore
    logger.info("Running in debugging mode.")
elif config.ENVIRONMENT == "development":
    format = "%(levelname)-8s %(filename)s:%(lineno)s %(funcName)s -> %(message)s"
    coloredlogs.install(level="DEBUG", logger=logger, fmt=format, level_styles=style)  # type: ignore
    logger.info("Running in debugging mode.")
else:
    format = "%(asctime)s %(name)s %(levelname)s %(message)s"
    coloredlogs.install(level="INFO", logger=logger, fmt=format, level_styles=style)  # type: ignore
    logger.info("Running in production mode.")
