import importlib
import logging

def load_instrument(class_path, name, params, simulate=False):
    if simulate:
        return MockInstrument(name, **params)

    module_name, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls(name=name, **params)

class MockInstrument:
    def __init__(self, name, **kwargs):
        self.name = name
        self.logger = logging.getLogger(f"Mock:{self.name}")
    def acquire_data(self):
        from datetime import datetime
        self.logger.info(f"[SIM] {self.name} acquiring fake data at {datetime.now()}")
    def read_realtime(self):
        return {"value": "simulated"}
    def set_config(self):
        self.logger.info(f"[SIM] Configuring {self.name}")