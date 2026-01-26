from __future__ import annotations
from typing import Any, Dict
from pydaq.instruments.instrument import Instrument

class AE33(Instrument):
    HEADERS = ["dtm", "bc", "atn"]
    def initialize(self) -> None: return
    def collect_record(self) -> Dict[str, Any]: return {}
