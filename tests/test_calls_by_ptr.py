# Copyright (c) 2022 Ilya Shchepetkov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

from clade import Clade
from tests.test_project import zero_c


def calls_by_ptr_is_ok(calls_by_ptr):
    assert calls_by_ptr[zero_c]["func_with_pointers"]["fp1"] == ["17"]
    assert calls_by_ptr[zero_c]["func_with_pointers"]["fp2"] == ["17"]


@pytest.mark.cif
def test_calls_by_ptr(tmpdir, cmds_file):
    conf = {"CmdGraph.requires": ["CC", "MV"]}

    c = Clade(tmpdir, cmds_file, conf)
    e = c.parse("CallsByPtr")

    calls_by_ptr = e.load_calls_by_ptr()

    calls_by_ptr_is_ok(calls_by_ptr)
