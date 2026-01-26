from __future__ import annotations
from typing import Any, Dict
from pydaq.instruments.instrument import Instrument

class FIDAS(Instrument):
    HEADERS = ["dtm", "pm10", "pm25", "pm1"]
    def initialize(self) -> None: return
    def collect_record(self) -> Dict[str, Any]: return {}
