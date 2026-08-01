from __future__ import annotations

import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

from pydaq.utils.transfer_handler import (
    SftpTarget,
    TransferHandler,
    TransferResult,
    TransferTarget,
)


def test_sftp_resources_close_when_put_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Both SFTP and SSH objects close after an upload exception."""

    class FakeSftp:
        def __init__(self) -> None:
            self.closed = False

        def stat(self, path: str) -> object:
            return object()

        def mkdir(self, path: str) -> None:
            return None

        def put(self, local: str, remote: str) -> None:
            raise OSError("simulated upload failure")

        def close(self) -> None:
            self.closed = True

    fake_sftp = FakeSftp()

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def set_missing_host_key_policy(self, policy: object) -> None:
            return None

        def connect(self, **kwargs: object) -> None:
            return None

        def open_sftp(self) -> FakeSftp:
            return fake_sftp

        def close(self) -> None:
            self.closed = True

    fake_client = FakeClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: fake_client,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    local_file = tmp_path / "sample.csv"
    local_file.write_text("test\n", encoding="utf-8")

    result = SftpTarget(
        host="example.invalid",
        user="test",
    ).upload(local_file, "instrument/sample.csv")

    assert result.ok is False
    assert fake_sftp.closed is True
    assert fake_client.closed is True


def test_ssh_client_closes_when_open_sftp_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The SSH client closes if SFTP initialization fails."""

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def set_missing_host_key_policy(self, policy: object) -> None:
            return None

        def connect(self, **kwargs: object) -> None:
            return None

        def open_sftp(self):
            raise OSError("simulated SFTP initialization failure")

        def close(self) -> None:
            self.closed = True

    fake_client = FakeClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: fake_client,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    local_file = tmp_path / "sample.csv"
    local_file.write_text("test\n", encoding="utf-8")

    result = SftpTarget(
        host="example.invalid",
        user="test",
    ).upload(local_file, "instrument/sample.csv")

    assert result.ok is False
    assert fake_client.closed is True


def test_global_and_instrument_scans_cannot_overlap(tmp_path: Path) -> None:
    """A global scan cannot overlap an active per-instrument scan."""
    started = Event()
    release = Event()

    class BlockingTarget(TransferTarget):
        kind = "blocking"

        def __init__(self) -> None:
            self.calls = 0

        def upload(
            self,
            local_path: Path,
            remote_relative_path: str,
        ) -> TransferResult:
            self.calls += 1
            started.set()
            assert release.wait(timeout=2.0)
            return TransferResult(
                True,
                self.kind,
                remote_relative_path,
            )

    target = BlockingTarget()

    first_outbox = tmp_path / "first"
    first_outbox.mkdir()
    (first_outbox / "first.zip").write_bytes(b"first")

    second_outbox = tmp_path / "second"
    second_outbox.mkdir()
    second_file = second_outbox / "second.zip"
    second_file.write_bytes(b"second")

    handler = TransferHandler(
        outbox_root=tmp_path,
        targets=[target],
        retries=1,
    )

    first_thread = Thread(
        target=handler.transmit_instrument,
        args=("first", "first"),
    )
    first_thread.start()

    assert started.wait(timeout=2.0)

    # This must return without starting another upload.
    replacement_handler = TransferHandler(
        outbox_root=tmp_path,
        targets=[target],
        retries=1,
    )
    global_thread = Thread(
        target=replacement_handler.transmit_all,
        args=(
            {"second": "second"},
            {"second": True},
        ),
    )
    global_thread.start()

    # The global scan waits; it cannot upload concurrently.
    assert target.calls == 1
    assert second_file.exists()

    release.set()
    first_thread.join(timeout=2.0)
    global_thread.join(timeout=2.0)

    assert not first_thread.is_alive()
    assert not global_thread.is_alive()
    assert target.calls == 2
    assert not second_file.exists()
