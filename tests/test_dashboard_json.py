import json
from pydaq.dashboard import _json_safe


def test_nonfinite_values_are_json_null():
    payload = {
        "ae33": {
            "latest": {
                "BC1": 12.5,
                "BC2": float("nan"),
                "BC3": float("inf"),
                "BC4": float("-inf"),
            }
        }
    }

    result = json.loads(
        json.dumps(_json_safe(payload), allow_nan=False)
    )

    assert result["ae33"]["latest"] == {
        "BC1": 12.5,
        "BC2": None,
        "BC3": None,
        "BC4": None,
    }