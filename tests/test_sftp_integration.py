from __future__ import annotations

"""Integration test for SFTP outbox transmission via the real PYDAQ machinery.

This test uses the same core transfer path as the orchestrator:

- build :class:`Orchestrator` from a real station YAML
- let it construct the real :class:`TransferHandler`
- write one probe file into a dedicated outbox instrument folder
- call ``TransferHandler.transmit_instrument(...)``
- verify that the uploaded file exists on every configured SFTP target

The station config path is provided on the pytest command line via:

    . .venv/bin/activate
    pytest -vv -rs -s tests/test_s3_integration.py --station-config ./pydaq/configs/nrb.yml
"""

import sys
import uuid
from pathlib import Path

import pytest
import schedule


pytestmark = pytest.mark.integration


def _load_orchestrator_class():
    """Import ``Orchestrator`` from the current PYDAQ checkout.

    Returns:
        The Orchestrator class.

    Raises:
        ImportError: If Orchestrator could not be imported.
    """
    repo_root = Path(__file__).resolve().parents[1]

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    errors: list[str] = []

    try:
        from pydaq.pydaq import Orchestrator

        return Orchestrator
    except Exception as exc:
        errors.append(f"from pydaq.pydaq import Orchestrator -> {exc!r}")

    try:
        from pydaq.pydaq import Orchestrator

        return Orchestrator
    except Exception as exc:
        errors.append(f"from pydaq import Orchestrator -> {exc!r}")

    joined = "\n".join(errors)
    raise ImportError(f"Could not import Orchestrator from the current PYDAQ checkout.\n{joined}")


@pytest.fixture()
def orchestrator(monkeypatch: pytest.MonkeyPatch, station_config_path: Path):
    """Build an Orchestrator while suppressing unrelated runtime side effects."""
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


def _get_paramiko_sftp_like_client(target):
    """Best-effort access to an underlying Paramiko-like SFTP client.

    Returns:
        A client object supporting ``listdir_attr()``, ``remove()``, and ``rmdir()``,
        or ``None`` if the target does not expose one.
    """
    candidate_attrs = (
        "_client",
        "client",
        "_sftp",
        "sftp",
        "_sftp_client",
        "sftp_client",
    )

    for attr in candidate_attrs:
        client = getattr(target, attr, None)
        if client is None:
            continue
        if all(hasattr(client, name) for name in ("listdir_attr", "remove", "rmdir")):
            return client

    return None


def _remote_rmtree(target, remote_dir: str) -> None:
    """Recursively delete a remote directory tree if possible.

    Falls back quietly if the target does not expose a directory-capable client.
    """
    client = _get_paramiko_sftp_like_client(target)
    if client is None:
        return

    def _walk(path: str) -> None:
        try:
            entries = client.listdir_attr(path)
        except Exception:
            return

        for entry in entries:
            child = f"{path.rstrip('/')}/{entry.filename}"
            mode = getattr(entry, "st_mode", 0)
            is_dir = bool(mode & 0o040000)

            if is_dir:
                _walk(child)
                try:
                    client.rmdir(child)
                except Exception:
                    pass
            else:
                try:
                    client.remove(child)
                except Exception:
                    pass

    _walk(remote_dir)

    try:
        client.rmdir(remote_dir)
    except Exception:
        pass


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

    all_targets = list(handler.targets)
    sftp_targets = [target for target in all_targets if getattr(target, "kind", "").lower() == "sftp"]
    if not sftp_targets:
        pytest.skip(f"No enabled SFTP target is configured in {station_config_path}.")

    # Force this test to exercise only the SFTP targets, even if the station config
    # mixes SFTP with S3 and/or requires all targets in production.
    original_targets = handler.targets
    original_require_all_targets = handler.require_all_targets
    handler.targets = sftp_targets
    handler.require_all_targets = False

    instrument_name = "__pytest_sftp__"
    remote_path = f"_pytest_sftp/{cfg.station.id}/{uuid.uuid4().hex}"

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

            # Remove the unique leaf directory and then the station test root if empty.
            try:
                target.delete(remote_path)
            except Exception:
                pass

            try:
                _remote_rmtree(target, remote_path)
            except Exception:
                pass

            try:
                target.delete(f"_pytest_sftp/{cfg.station.id}")
            except Exception:
                pass

            try:
                _remote_rmtree(target, f"_pytest_sftp/{cfg.station.id}")
            except Exception:
                pass

            try:
                target.delete("_pytest_sftp")
            except Exception:
                pass

            try:
                _remote_rmtree(target, "_pytest_sftp")
            except Exception:
                pass

        local_path.unlink(missing_ok=True)

        try:
            outbox_dir.rmdir()
        except OSError:
            pass

        handler.targets = original_targets
        handler.require_all_targets = original_require_all_targets