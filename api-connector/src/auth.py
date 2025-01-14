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

from requests.auth import AuthBase


class TokenAuth(AuthBase):
    def __init__(self, token, auth_scheme="Bearer"):
        self.token = token
        self.auth_scheme = auth_scheme

    def __call__(self, request):
        request.headers["Authorization"] = f"{self.auth_scheme} {self.token}"
        return request
