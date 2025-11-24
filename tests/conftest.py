from __future__ import annotations

import io
import logging
import os
import threading
import types
from pathlib import Path, PurePosixPath
from typing import Dict, Tuple

import paramiko  # only for policy types referenced by SFTPClient
import pytest
import schedule


# -----------------------
# In-memory remote FS
# -----------------------
class _Attr:
    def __init__(self, st_size: int):
        self.st_size = st_size


class MemorySFTP:
    """
    A minimal in-memory SFTP surface that supports the methods the
    refactored SFTPClient uses: chdir, mkdir, listdir, put, stat,
    remove, rmdir. Paths are POSIX (str).
    """
    def __init__(self, fs):
        # fs: {'dirs': set[str], 'files': dict[str, int], 'cwd': str}
        self.fs = fs

    def _norm(self, p: str) -> str:
        pp = PurePosixPath(p).as_posix()
        if pp == ".":
            pp = self.fs["cwd"]
        # Collapse duplicate slashes
        pp = PurePosixPath(pp).as_posix()
        return pp

    def _ensure_parent_exists(self, path: str):
        parent = str(PurePosixPath(path).parent)
        if parent not in self.fs["dirs"]:
            raise IOError(f"Parent does not exist: {parent}")

    def chdir(self, path: str):
        p = self._norm(path)
        if p == "/":
            self.fs["cwd"] = "/"
            self.fs["dirs"].add("/")
            return
        if p not in self.fs["dirs"]:
            raise IOError(f"No such directory: {p}")
        self.fs["cwd"] = p

    def mkdir(self, path: str, mode: int = 0o755):
        p = self._norm(path)
        parent = str(PurePosixPath(p).parent)
        if p in self.fs["dirs"]:
            return
        if parent != p and parent not in self.fs["dirs"]:
            raise IOError(f"Parent does not exist: {parent}")
        self.fs["dirs"].add(p)

    def listdir(self, path: str):
        p = self._norm(path)
        if p not in self.fs["dirs"]:
            raise IOError(f"No such directory: {p}")
        names = set()
        plen = len(p.rstrip("/"))
        for d in self.fs["dirs"]:
            if d == p:
                continue
            if d.startswith(p.rstrip("/") + "/"):
                tail = d[plen + 1 :]
                if "/" not in tail and tail:
                    names.add(tail)
        for f in self.fs["files"]:
            if f.startswith(p.rstrip("/") + "/"):
                tail = f[plen + 1 :]
                # only immediate children (no slash)
                if "/" not in tail and tail:
                    names.add(tail)
        return sorted(names)

    def put(self, localpath: str, remotepath: str, confirm: bool = True):
        rp = self._norm(remotepath)
        self._ensure_parent_exists(rp)
        size = os.stat(localpath).st_size
        self.fs["files"][rp] = size
        return _Attr(st_size=size)

    def stat(self, path: str):
        p = self._norm(path)
        if p in self.fs["files"]:
            return _Attr(st_size=self.fs["files"][p])
        if p in self.fs["dirs"]:
            # Dir; size not meaningful; return dummy
            return _Attr(st_size=0)
        raise FileNotFoundError(p)

    def remove(self, path: str):
        p = self._norm(path)
        if p not in self.fs["files"]:
            raise FileNotFoundError(p)
        del self.fs["files"][p]

    def rmdir(self, path: str):
        p = self._norm(path)
        # only allow if empty
        # no files under p
        for f in list(self.fs["files"].keys()):
            if f.startswith(p.rstrip("/") + "/"):
                raise IOError("Directory not empty")
        # no subdirectories under p
        for d in list(self.fs["dirs"]):
            if d != p and d.startswith(p.rstrip("/") + "/"):
                raise IOError("Directory not empty")
        if p not in self.fs["dirs"]:
            raise IOError("No such directory")
        self.fs["dirs"].remove(p)

    def close(self):
        pass


class MemorySSHClient:
    """
    Minimal stand-in for paramiko.SSHClient.
    Each instance shares the same fs dict (provided by factory) so
    multiple connections see the same remote state.
    """
    def __init__(self, fs):
        self.fs = fs
        self.policy = None
        self.connected = False

    def set_missing_host_key_policy(self, policy):
        # store class name to allow assertions if needed
        self.policy = policy.__class__.__name__

    def load_system_host_keys(self):
        # no-op
        pass

    def connect(
        self,
        hostname=None,
        username=None,
        pkey=None,
        password=None,
        timeout=None,
        auth_timeout=None,
        banner_timeout=None,
        look_for_keys=None,
        sock=None,
    ):
        # ignore params, just mark connected
        self.connected = True
        # ensure root exists
        self.fs["dirs"].add("/")
        self.fs["cwd"] = "/"

    def open_sftp(self):
        if not self.connected:
            raise RuntimeError("Not connected")
        return MemorySFTP(self.fs)

    def close(self):
        self.connected = False


class DummyNeph:
    """Minimal stand-in for Instrument/NEPH for protocol tests."""

    def __init__(self) -> None:
        self._name = "dummy_neph"
        self.logger = logging.getLogger("dummy_neph")
        self._sockaddr = ("127.0.0.1", 3602)
        self._socktout = 1.0
        self._socksleep = 0.0
        self._io_lock = threading.Lock()
        self._params_comms = "socket"
        self.sampling_interval = 60  # seconds

    def _use_serial(self) -> bool:
        """Mirror NEPH._use_serial: True if configured for serial comms."""
        return self._params_comms == "serial"

    # These are used only by AuroraClient, and we’ll override them in tests.
    def _serial_comm(self, cmd: str) -> str:  # pragma: no cover - overridden in tests
        raise RuntimeError("Not used in dummy.")

    def _socket_comm(self, cmd: str) -> str:  # pragma: no cover - overridden in tests
        raise RuntimeError("Not used in dummy.")


# -----------------------
# Fixtures
# -----------------------
@pytest.fixture
def logger():
    lg = logging.getLogger("sftp-tests")
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        lg.addHandler(logging.StreamHandler(io.StringIO()))
    return lg


@pytest.fixture
def remote_fs():
    """Fresh in-memory remote filesystem."""
    return {"dirs": set(["/"]), "files": {}, "cwd": "/"}


@pytest.fixture
def monkeypatch_paramiko(monkeypatch, remote_fs):
    """
    Replace paramiko.SSHClient with our memory client sharing a remote fs.
    """
    def ssh_factory():
        return MemorySSHClient(remote_fs)

    monkeypatch.setattr(
        paramiko, "SSHClient", ssh_factory, raising=True
    )
    # Return the shared fs so tests can introspect
    return remote_fs


@pytest.fixture(autouse=True)
def clear_schedule_jobs():
    schedule.clear()
    yield
    schedule.clear()


@pytest.fixture
def config(tmp_path):
    return {
        "host": "example.test",
        "usr": "tester",
        "staging": str(tmp_path / "staging"),
        "remote": "/upload/base",
        "accept_unknown_host_keys": True,  # keep simple in tests
    }


@pytest.fixture
def dummy_driver() -> DummyNeph:
    return DummyNeph()
