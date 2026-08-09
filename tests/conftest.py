from __future__ import annotations

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine

# 128MB: big enough for FTS index builds, small enough to keep the suite's
# many concurrent Engine instances light.
TEST_BUFFER_POOL = 128 * 1024 * 1024


@pytest.fixture()
def engine(tmp_path):
    eng = Engine(
        GragConfig(db_path=tmp_path / "test.lbdb", buffer_pool_size=TEST_BUFFER_POOL)
    )
    yield eng
    eng.close()


@pytest.fixture()
def memory_engine():
    eng = Engine(GragConfig(db_path=":memory:", buffer_pool_size=TEST_BUFFER_POOL))
    yield eng
    eng.close()
