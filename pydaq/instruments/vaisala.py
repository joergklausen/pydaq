from __future__ import annotations
from typing import Any, Dict
from pydaq.instruments.instrument import Instrument

class HMP110ASCII(Instrument):
    HEADERS = ["dtm", "t", "rh", "td"]
    def initialize(self) -> None: return
    def collect_record(self) -> Dict[str, Any]: return {}
