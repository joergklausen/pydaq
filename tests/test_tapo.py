"""Unit tests for the OpenCV Tapo file-source driver (no physical camera)."""
import logging
from pathlib import Path
from queue import Queue
from threading import Lock
from types import SimpleNamespace

import cv2
import pytest

from pydaq.instruments.tapo import TAPO


def camera(tmp_path):
    instance = object.__new__(TAPO)
    instance.name = "tapo"
    instance.logger = logging.getLogger("test-tapo")
    instance.parameters = {
        "io": {"kind": "rtsp", "host": "192.168.200.164", "port": 554,
               "username": str(tmp_path / "user.txt"),
               "password": str(tmp_path / "pass.txt"),
               "timeout_seconds": 10, "retries": 2},
        "init": {"stream": 1, "image_format": "png", "warmup_reads": 3},
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


class FakeCapture:
    def __init__(self, opened=True, frames=None):
        self.opened = opened
        self.frames = iter(frames if frames is not None else [(True, object())])
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        return next(self.frames, (False, None))

    def release(self):
        self.released = True


def setup_cv(monkeypatch, opened=True):
    seen = []

    def fake_open(*args):
        capture = FakeCapture(opened=opened)
        seen.append((args, capture))
        return capture

    def fake_write(path, frame, options):
        Path(path).write_bytes(b"FAKE IMAGE")
        return True

    monkeypatch.setattr(cv2, "VideoCapture", fake_open)
    monkeypatch.setattr(cv2, "imwrite", fake_write)
    monkeypatch.setattr("pydaq.instruments.tapo.time.sleep", lambda x: None)
    return seen


def test_capture_and_transfer_staging(tmp_path, monkeypatch):
    d = camera(tmp_path)
    seen = setup_cv(monkeypatch)
    d.request_reading()
    archive = d._task_queue.get_nowait()()
    assert archive.is_file()
    assert archive.suffix == ".png"
    assert archive.read_bytes() == (d.outbox_dir / archive.name).read_bytes()
    assert d.state.latest["file"] == archive.name
    assert not list((d.outbox_dir / ".incoming").iterdir())
    assert len(seen) == 1
    assert seen[0][0][0] == "rtsp://camera:secret@192.168.200.164:554/stream1"
    assert seen[0][0][1] == cv2.CAP_FFMPEG
    assert seen[0][1].released


def test_missing_credentials_no_capture(tmp_path, monkeypatch):
    d = camera(tmp_path)
    (tmp_path / "pass.txt").unlink()
    seen = setup_cv(monkeypatch)
    assert d.capture_snapshot() is None
    assert not seen
    assert not list(tmp_path.rglob("*.png"))


def test_open_failure_does_not_stage(tmp_path, monkeypatch):
    d = camera(tmp_path)
    seen = setup_cv(monkeypatch, opened=False)
    assert d.capture_snapshot() is None
    assert len(seen) == 2
    assert all(cap.released for _, cap in seen)
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
    seen = setup_cv(monkeypatch)
    archive = d.capture_snapshot()
    assert archive.suffix == ".jpg"
    assert (d.outbox_dir / archive.name).is_file()


def test_disabled_skips_capture(tmp_path, monkeypatch):
    d = camera(tmp_path)
    d.state.enabled = False
    seen = setup_cv(monkeypatch)
    assert d.capture_snapshot() is None
    assert not seen


def test_temp_files_cleaned_on_write_failure(tmp_path, monkeypatch):
    d = camera(tmp_path)
    setup_cv(monkeypatch)
    monkeypatch.setattr(cv2, "imwrite", lambda *_: False)
    assert d.capture_snapshot() is None
    assert not list(tmp_path.rglob("*.partial"))
    assert not list(tmp_path.rglob("*.png"))
