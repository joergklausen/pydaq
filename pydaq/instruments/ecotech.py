from __future__ import annotations
from typing import Any, Dict
from instruments.instrument import Instrument

class NEPH(Instrument):
    HEADERS = ["dtm", "ssp", "bssp"]
    def initialize(self) -> None: return
    def collect_record(self) -> Dict[str, Any]: return {}
