from __future__ import annotations
from typing import Any, Dict
from pydaq.instruments.instrument import Instrument

class G2401(Instrument):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, headers=None, **kwargs)
    def sample_once(self) -> None: return
    def collect_record(self) -> Dict[str, Any]: return {}
