from __future__ import annotations

from types import SimpleNamespace

from pydaq.pydaq import Orchestrator


def test_wildcard_udp_listener_is_not_treated_as_remote_instrument() -> None:
    orchestrator = object.__new__(Orchestrator)
    config = SimpleNamespace(
        instruments={
            "fidas": SimpleNamespace(
                enabled=True,
                io={"kind": "udp", "host": "0.0.0.0", "port": 56790},
            ),
            "ne300": SimpleNamespace(
                enabled=True,
                io={"kind": "tcp", "host": "192.168.3.149", "port": 32783},
            ),
        }
    )

    targets = orchestrator._build_network_monitor_targets(config)
    assert [target.name for target in targets] == ["ne300"]
    assert targets[0].host == "192.168.3.149"
    assert targets[0].port == 32783


def test_network_tick_seeds_down_state_after_driver_already_reported_error() -> None:
    target = SimpleNamespace(name="ne300", host="192.168.3.149", port=32783)

    class FakeMonitor:
        def __init__(self) -> None:
            self.targets = [target]
            self._state = {}
            self._ticks = {}
            self.seen_state = None

        @staticmethod
        def _key(value) -> str:
            return f"{value.name}@{value.host}:{value.port}"

        def check_all(self) -> None:
            self.seen_state = dict(self._state)

    orchestrator = object.__new__(Orchestrator)
    orchestrator._network_monitor = FakeMonitor()
    orchestrator.instruments = {
        "ne300": SimpleNamespace(
            state=SimpleNamespace(last_error="unavailable: tcp 192.168.3.149:32783 timed out")
        )
    }
    orchestrator.logger = SimpleNamespace(exception=lambda *args, **kwargs: None)

    orchestrator._network_monitor_tick()
    assert orchestrator._network_monitor.seen_state == {
        "ne300@192.168.3.149:32783": False
    }
