from __future__ import annotations

from pathlib import Path

import pytest

from pydaq.utils.single_instance_lock import (
    AlreadyRunningError,
    SingleInstanceLock,
    canonical_config_path,
)


def test_canonical_config_path_is_absolute(tmp_path: Path) -> None:
    cfg = tmp_path / "station.yml"
    assert canonical_config_path(cfg).is_absolute()


def test_second_lock_for_same_config_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "station.yml"
    cfg.write_text("station:\n  id: test\n", encoding="utf-8")

    first = SingleInstanceLock(cfg)
    second = SingleInstanceLock(cfg)

    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError) as exc_info:
            second.acquire()
        assert canonical_config_path(cfg) == exc_info.value.config_path
    finally:
        first.release()
        second.release()


def test_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    cfg = tmp_path / "station.yml"
    cfg.write_text("station:\n  id: test\n", encoding="utf-8")

    first = SingleInstanceLock(cfg)
    first.acquire()
    first.release()

    second = SingleInstanceLock(cfg)
    second.acquire()
    assert second.acquired
    second.release()


def test_different_configs_can_run_concurrently(tmp_path: Path) -> None:
    cfg_a = tmp_path / "a.yml"
    cfg_b = tmp_path / "b.yml"
    cfg_a.write_text("station:\n  id: a\n", encoding="utf-8")
    cfg_b.write_text("station:\n  id: b\n", encoding="utf-8")

    with SingleInstanceLock(cfg_a), SingleInstanceLock(cfg_b):
        pass


def test_context_manager_releases_after_exception(tmp_path: Path) -> None:
    cfg = tmp_path / "station.yml"
    cfg.write_text("station:\n  id: test\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        with SingleInstanceLock(cfg):
            raise RuntimeError("boom")

    with SingleInstanceLock(cfg):
        pass


def test_lock_blocks_another_process(tmp_path: Path) -> None:
    import subprocess
    import sys

    cfg = tmp_path / "station.yml"
    cfg.write_text("station:\n  id: test\n", encoding="utf-8")

    holder_code = r'''
import sys
from pydaq.utils.single_instance_lock import SingleInstanceLock
lock = SingleInstanceLock(sys.argv[1])
lock.acquire()
print("LOCKED", flush=True)
sys.stdin.readline()
lock.release()
'''
    contender_code = r'''
import sys
from pydaq.utils.single_instance_lock import AlreadyRunningError, SingleInstanceLock
try:
    SingleInstanceLock(sys.argv[1]).acquire()
except AlreadyRunningError:
    raise SystemExit(23)
raise SystemExit(0)
'''

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(cfg)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "LOCKED"
        contender = subprocess.run(
            [sys.executable, "-c", contender_code, str(cfg)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert contender.returncode == 23, contender.stderr
    finally:
        if holder.stdin is not None:
            holder.stdin.write("release\n")
            holder.stdin.flush()
        holder.wait(timeout=10)
