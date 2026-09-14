"""Exercise real bindings on both backends, including the Windows wheel gate."""
from __future__ import annotations

import pytest

from grag.core.engine import Engine


@pytest.mark.parametrize("values", [
    [0, 128], [128, 0], [-129, 0, 32768], [0, 2**31],
    [-(2**63), 2**63 - 1], [None, 0, 128, None],
    [[0, 128], [32768, 0]], [True, False], [0.0, 128.5],
    ["zero", "one"],
])
def test_list_parameters_round_trip_without_changing_payload(engine, values):
    import copy

    parameters = {"values": values}
    before = copy.deepcopy(parameters)
    assert engine.execute("RETURN $values", parameters).rows == [[values]]
    assert parameters == before
    assert [type(v) for v in values] == [type(v) for v in before["values"]]


def test_integer_lists_in_struct_parameters(engine):
    value = {"codes": [0, 128, 255], "enabled": True, "count": 5}
    assert engine.execute("RETURN $value", {"value": value}).rows == [[value]]


def test_integer_list_writes_rollback_and_survive_strict_reopen(engine):
    engine.execute_write("CREATE NODE TABLE Codes(id STRING PRIMARY KEY, codes UINT8[])")
    values = [0, 127, 128, 255]
    with engine.write_transaction():
        engine.execute_write("CREATE (:Codes {id:'kept', codes:$values})", {"values": values})
    with pytest.raises(ValueError, match="abort"), engine.write_transaction():
        engine.execute_write("CREATE (:Codes {id:'discarded', codes:$values})", {"values": values})
        raise ValueError("abort")
    engine.close()
    with Engine(engine.config) as reopened:
        assert reopened.execute("MATCH (n:Codes) RETURN n.id,n.codes").rows == [["kept", values]]


def test_capi_integer_overflow_is_not_silently_wrapped(engine):
    if engine.runtime_info()["backend"] != "capi":
        pytest.skip("C-API integer conversion bound")
    from grag.core.errors import CypherError

    # NumPy uses "int too big to convert" on Windows and "too large" on POSIX.
    with pytest.raises(CypherError, match=r"(large|big|overflow)"):
        engine.execute("RETURN $values", {"values": [0, 2**64]})
    assert engine.execute("RETURN 42").rows == [[42]]
