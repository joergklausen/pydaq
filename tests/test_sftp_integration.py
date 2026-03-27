from __future__ import annotations

"""Integration test for SFTP outbox transmission via the real PYDAQ machinery.

This test uses the same core transfer path as the orchestrator:

- build :class:`Orchestrator` from a real station YAML
- let it construct the real :class:`TransferHandler`
- write one probe file into a dedicated outbox instrument folder
- call ``TransferHandler.transmit_instrument(...)``
- verify that the uploaded file exists on every configured SFTP target

The station config path is provided on the pytest command line via:

    pytest ... --station-config /path/to/station.yml
"""

import importlib
import importlib.util
import uuid
from pathlib import Path

import pytest
import schedule


pytestmark = pytest.mark.integration


def _load_orchestrator_class():
    """Import ``Orchestrator`` from the current PYDAQ checkout.

    Tries the package layout first, then falls back to loading the module
    from ``pydaq/pydaq.py`` or ``pydaq.py`` directly.

    Returns:
        The Orchestrator class.

    Raises:
        ImportError: If Orchestrator could not be imported from any expected location.
    """
    errors: list[str] = []

    # Most likely layout for your repository.
    for module_name in ("pydaq.pydaq", "pydaq"):
        try:
            module = importlib.import_module(module_name)
            orchestrator = getattr(module, "Orchestrator", None)
            if orchestrator is not None:
                return orchestrator
            errors.append(f"{module_name}: imported, but no Orchestrator attribute found")
        except Exception as exc:
            errors.append(f"{module_name}: {exc!r}")

    # Fallback: import from file path.
    repo_root = Path(__file__).resolve().parents[1]
    for module_path in (
        repo_root / "pydaq" / "pydaq.py",
        repo_root / "pydaq.py",
    ):
        if not module_path.exists():
            errors.append(f"{module_path}: file not found")
            continue

        try:
            spec = importlib.util.spec_from_file_location("pydaq_main_module", module_path)
            if spec is None or spec.loader is None:
                errors.append(f"{module_path}: could not create import spec")
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            orchestrator = getattr(module, "Orchestrator", None)
            if orchestrator is not None:
                return orchestrator
            errors.append(f"{module_path}: imported, but no Orchestrator attribute found")
        except Exception as exc:
            errors.append(f"{module_path}: {exc!r}")

    joined = "\n".join(errors)
    raise ImportError(f"Could not import Orchestrator from the current PYDAQ checkout.\n{joined}")


@pytest.fixture()
def orchestrator(monkeypatch: pytest.MonkeyPatch, station_config_path: Path):
    """Build an Orchestrator while suppressing unrelated runtime side effects.

    We want the real config loading and real transfer-handler construction,
    but we do not want this integration test to:
    - start instrument threads
    - start the dashboard
    - run the startup transfer self-test
    - start or refresh the network monitor
    """
    Orchestrator = _load_orchestrator_class()

    monkeypatch.setattr(Orchestrator, "_startup_transfer_selftest", lambda self: None)
    monkeypatch.setattr(Orchestrator, "_apply_configuration", lambda self, config: None)
    monkeypatch.setattr(Orchestrator, "_start_dashboard", lambda self: None)
    monkeypatch.setattr(Orchestrator, "_refresh_network_monitor", lambda self, config: None)

    orch = Orchestrator(station_config_path)

    try:
        yield orch
    finally:
        schedule.clear()


def test_sftp_transmit_from_outbox_uses_same_transfer_machinery_as_orchestrator(
    orchestrator,
    station_config_path: Path,
) -> None:
    """Upload one probe file from a synthetic outbox folder to configured SFTP target(s)."""
    cfg = orchestrator.application_config
    assert cfg is not None, "Orchestrator did not load an application config."

    handler = orchestrator.transfer_handler
    if handler is None:
        pytest.skip("Transfer is disabled or no enabled transfer targets were built.")

    sftp_targets = [target for target in handler.targets if getattr(target, "kind", "").lower() == "sftp"]
    if not sftp_targets:
        pytest.skip(f"No enabled SFTP target is configured in {station_config_path}.")

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
                f"target={target!r} "
                f"remote_relative_path={remote_relative_path!r} "
                f"detail={exists_result.detail!r}"
            )

        assert not local_path.exists(), "Local probe file still exists although upload succeeded."

    finally:
        for target in sftp_targets:
            try:
                target.delete(remote_relative_path)
            except Exception:
                pass

        local_path.unlink(missing_ok=True)