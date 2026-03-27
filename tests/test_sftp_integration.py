"""Integration test for SFTP outbox transmission via the real PYDAQ machinery.

This test intentionally goes through the same configuration-loading and transfer-handler
construction path as the orchestrator itself: :class:`Orchestrator` loads the YAML config
and builds its :class:`TransferHandler`, which in turn uses the configured
:class:`SftpTarget` implementation for upload / exists / delete operations.

The test is intentionally conservative:

- It uses an explicit path to a real ``station.yml`` file.
- It suppresses unrelated orchestrator side effects (instrument startup, dashboard,
  network monitor, startup self-test) so the test exercises transfer logic only.
- It writes exactly one probe file into a dedicated synthetic outbox folder, then calls
  ``TransferHandler.transmit_instrument(...)`` on that folder.
- It verifies that the remote object exists on every configured SFTP target.

Recommended usage
-----------------
Adjust ``STATION_CONFIG_PATH`` below to point at your integration-test station config,
then run:

    pytest -m integration -q tests/test_sftp_integration.py

Notes
-----
If your config also enables non-SFTP targets and ``require_all_targets=True``, this test
skips on purpose. In that situation a failure in a non-SFTP sink could prevent local
cleanup even if SFTP worked, which would make an SFTP-specific integration test ambiguous.
"""

from __future__ import annotations

import importlib
import importlib.util
import uuid
from pathlib import Path

import pytest
import schedule


pytestmark = pytest.mark.integration

# Set this to the station YAML you want this integration test to use.
STATION_CONFIG_PATH = Path(__file__).resolve().parents[1] / "buc.yml"


def _load_orchestrator_class():
    """Import ``Orchestrator`` robustly from the local project.

    This helper tries a few likely import locations so the test can work whether the
    orchestrator lives as a top-level ``pydaq.py`` module or inside a package layout.
    """
    candidates: list[tuple[str, str]] = [
        ("pydaq", "Orchestrator"),
        ("pydaq.pydaq", "Orchestrator"),
    ]

    for module_name, attr_name in candidates:
        try:
            module = importlib.import_module(module_name)
            orchestrator = getattr(module, attr_name, None)
            if orchestrator is not None:
                return orchestrator
        except Exception:
            continue

    repo_root = Path(__file__).resolve().parents[1]
    pydaq_script = repo_root / "pydaq.py"
    if pydaq_script.exists():
        spec = importlib.util.spec_from_file_location("pydaq_main_module", pydaq_script)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            orchestrator = getattr(module, "Orchestrator", None)
            if orchestrator is not None:
                return orchestrator

    raise ImportError("Could not import Orchestrator from the current PYDAQ checkout.")


@pytest.fixture(scope="session")
def config_path() -> Path:
    """Return the explicit station config path or skip the test."""
    path = STATION_CONFIG_PATH.expanduser()
    if not path.is_file():
        pytest.skip(f"station.yml not found at configured path: {path}")
    return path


@pytest.fixture()
def orchestrator(monkeypatch: pytest.MonkeyPatch, config_path: Path):
    """Build an orchestrator with transfer setup intact but unrelated runtime effects disabled."""
    Orchestrator = _load_orchestrator_class()

    monkeypatch.setattr(Orchestrator, "_startup_transfer_selftest", lambda self: None)
    monkeypatch.setattr(Orchestrator, "_apply_configuration", lambda self, config: None)
    monkeypatch.setattr(Orchestrator, "_start_dashboard", lambda self: None)
    monkeypatch.setattr(Orchestrator, "_refresh_network_monitor", lambda self, config: None)

    orch = Orchestrator(config_path)

    try:
        yield orch
    finally:
        schedule.clear()


def test_sftp_transmit_from_outbox_uses_same_transfer_machinery_as_orchestrator(
    orchestrator,
    config_path: Path,
) -> None:
    """Upload one probe file from a synthetic outbox folder to the configured SFTP sink(s).

    This test reuses the ``TransferHandler`` built by :class:`Orchestrator` from the real
    YAML config, then exercises its normal ``transmit_instrument()`` scan of an outbox
    directory.
    """
    cfg = orchestrator.application_config
    assert cfg is not None, "Orchestrator did not load an application config."

    handler = orchestrator.transfer_handler
    if handler is None:
        pytest.skip("Transfer is disabled or no enabled transfer targets were built.")

    sftp_targets = [target for target in handler.targets if getattr(target, "kind", "").lower() == "sftp"]
    if not sftp_targets:
        pytest.skip(f"No enabled SFTP target is configured in {config_path!s}.")

    non_sftp_targets = [target for target in handler.targets if getattr(target, "kind", "").lower() != "sftp"]
    if non_sftp_targets and handler.require_all_targets:
        pytest.skip(
            "Config mixes SFTP with other required targets. "
            "Use an SFTP-only integration config for this test."
        )

    instrument_name = "__pytest_sftp__"
    remote_path = f"_pytest_sftp/{cfg.station.id}"

    outbox_dir = cfg.paths.outbox / instrument_name
    outbox_dir.mkdir(parents=True, exist_ok=True)

    filename = f"pytest_sftp_probe_{uuid.uuid4().hex}.txt"
    local_path = outbox_dir / filename
    payload = (
        "pydaq pytest sftp integration probe\n"
        f"station={cfg.station.id}\n"
        f"filename={filename}\n"
    )
    local_path.write_text(payload, encoding="utf-8")

    remote_relative_path = handler._build_remote_relative_path(remote_path, filename)

    try:
        handler.transmit_instrument(
            instrument_name=instrument_name,
            remote_path=remote_path,
            remove_on_success=True,
        )

        for target in sftp_targets:
            exists_result = target.exists(remote_relative_path)
            assert exists_result.ok, (
                "Probe file was not found on the configured SFTP target. "
                f"target={target!r} remote_relative_path={remote_relative_path!r} "
                f"detail={exists_result.detail!r}"
            )

        assert not local_path.exists(), "Local probe file still exists although upload succeeded."

    finally:
        for target in sftp_targets:
            try:
                target.delete(remote_relative_path)
            except Exception:
                # Best effort only; the test result should be driven by upload/verify.
                pass

        local_path.unlink(missing_ok=True)
