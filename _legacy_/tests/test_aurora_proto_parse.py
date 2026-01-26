# tests/instr/test_aurora_proto_parse.py
from datetime import datetime

import numpy as np

from instr.ecotech.aurora_proto import AuroraClient
from tests.conftest import DummyNeph  # or import from there


def test_parse_current_data():
    drv = DummyNeph()
    client = AuroraClient(drv, params={"serial_id": 1}) # type: ignore

    line = "2025-01-01 00:00:00, 1.0, 2.0, 0A"
    ts, values = client.parse_current_data(line)

    assert isinstance(ts, datetime)
    assert ts.year == 2025
    assert ts.month == 1
    assert ts.day == 1

    assert isinstance(values, np.ndarray)
    assert values.shape == (3,)
    assert values[0] == 1.0
    assert values[1] == 2.0
    assert values[2] == int("0A", 16)
