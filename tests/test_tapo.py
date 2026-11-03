"""Run with pytest in the pydaq checkout. FFmpeg and the network are mocked."""
from pathlib import Path
from threading import Lock
from queue import Queue
from types import SimpleNamespace
import logging

from pydaq.instruments.tapo import TAPO


def camera(tmp_path):
    instance = object.__new__(TAPO)
    instance.name = "tapo"
    instance.logger = logging.getLogger("test-tapo")
    instance.parameters = {
        "io": {"kind": "rtsp", "host": "192.168.200.164", "port": 554,
               "username": str(tmp_path / "user.txt"),
               "password": str(tmp_path / "pass.txt")},
        "init": {"stream": 1, "image_format": "png"},
    }
    (tmp_path / "user.txt").write_text("camera\n")
    (tmp_path / "pass.txt").write_text("secret\n")
    instance.data_dir = tmp_path / "data" / "tapo"
    instance.outbox_dir = tmp_path / "outbox" / "tapo"
    instance._capture_lock = Lock()
    instance._task_queue = Queue()
    instance._state_lock = Lock()
    instance.state = SimpleNamespace(enabled=True, latest={}, last_error="", last_sample_ts=0)
    return instance


def test_capture_and_transfer_staging(tmp_path, monkeypatch):
    d = camera(tmp_path)
    monkeypatch.setattr("pydaq.instruments.tapo.shutil.which", lambda v: "/usr/bin/ffmpeg")
    def fake_run(cmd, **kwargs):
        assert "rtsp://camera:secret@192.168.200.164:554/stream1" in cmd
        Path(cmd[-1]).write_bytes(b"FAKE PNG")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("pydaq.instruments.tapo.subprocess.run", fake_run)
    d.request_reading()
    archive = d._task_queue.get_nowait()()
    assert archive.is_file()
    assert archive.suffix == ".png"
    assert archive.read_bytes() == (d.outbox_dir / archive.name).read_bytes()
    assert d.state.latest["file"] == archive.name
    assert not list((d.outbox_dir / ".incoming").iterdir())


def test_missing_credentials_no_capture(tmp_path):
    d = camera(tmp_path)
    (tmp_path / "pass.txt").unlink()
    assert d.capture_snapshot() is None
    assert not list(tmp_path.rglob("*.png"))


def test_failure_does_not_stage(tmp_path, monkeypatch):
    d = camera(tmp_path)
    monkeypatch.setattr("pydaq.instruments.tapo.shutil.which", lambda v: "/usr/bin/ffmpeg")
    monkeypatch.setattr("pydaq.instruments.tapo.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert d.capture_snapshot() is None
    assert not list(tmp_path.rglob("*.png"))


def test_lock_skips_overlapping(tmp_path):
    d = camera(tmp_path)
    d._capture_lock.acquire()
    try:
        assert d.capture_snapshot() is None
    finally:
        d._capture_lock.release()


def test_jpg_output(tmp_path, monkeypatch):
    d = camera(tmp_path)
    d.parameters["init"]["image_format"] = "jpg"
    monkeypatch.setattr("pydaq.instruments.tapo.shutil.which", lambda v: "/usr/bin/ffmpeg")
    def fake_run(cmd, **kwargs):
        assert cmd[-1].endswith(".jpg")
        Path(cmd[-1]).write_bytes(b"FAKE JPG")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("pydaq.instruments.tapo.subprocess.run", fake_run)
    assert d.capture_snapshot().suffix == ".jpg"
