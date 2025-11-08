from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import schedule

from utils.sftp import SFTPClient  # your refactored class


def _write(tmp: Path, rel: str, data: bytes = b"hello") -> Path:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def client(config, logger, monkeypatch_paramiko, tmp_path):
    # Prepare local staging with a few files
    staging = Path(config["staging"])
    staging.mkdir(parents=True, exist_ok=True)
    _write(staging, "a.txt", b"a" * 3)
    _write(staging, "sub/one.bin", b"x" * 10)
    _write(staging, "sub/two.bin", b"y" * 20)

    return SFTPClient(config=config, logger=logger)


def test_is_alive_true(client):
    assert client.is_alive() is True


def test_list_local_files_recursive(client, config):
    base = Path(config["staging"])
    files = {p.relative_to(base).as_posix() for p in client.list_local_files()}
    assert files == {"a.txt", "sub/one.bin", "sub/two.bin"}


def test_put_file_creates_dirs_and_deletes_local(tmp_path, client, monkeypatch_paramiko):
    # fresh local file outside staging
    lf = _write(tmp_path, "foo/bar/baz.dat", b"Z" * 42)
    remote_dir = PurePosixPath("/upload/base/new/deep")

    remote_file = client.put_file(lf, remote_dir, remove_on_success=True)
    assert remote_file == remote_dir / lf.name

    # local file deleted on success
    assert not lf.exists()

    # recorded
    assert any(p.endswith("/baz.dat") for p in client.transferred_local)
    assert any(p.endswith("/new/deep/baz.dat") for p in client.transferred_remote)


def test_setup_remote_folders_mirrors_directory_tree(client, config, monkeypatch_paramiko):
    # staging already has files; ensure directory skeleton exists remotely
    client.setup_remote_folders()

    # Verify via list_remote_items
    children = client.list_remote_items("/upload/base")
    # 'sub' should exist as a directory, 'a.txt' is a file located at the base
    assert "sub" in children or "sub" in children  # basic presence check
    # Note: list_remote_items returns immediate names (files and dirs)


def test_transfer_files_mirrors_and_keeps_local(client, config, monkeypatch_paramiko):
    client.transfer_files(remove_on_success=False)

    # Remote should now contain the uploaded files
    names = set(client.list_remote_items("/upload/base"))
    assert "a.txt" in names
    assert "sub" in names

    sub_names = set(client.list_remote_items("/upload/base/sub"))
    assert {"one.bin", "two.bin"} <= sub_names

    # Local files still present
    base = Path(config["staging"])
    assert (base / "a.txt").exists()
    assert (base / "sub/one.bin").exists()

    # Now remove_on_success=True deletes local files when sizes match
    client.transfer_files(remove_on_success=True)
    assert not (base / "a.txt").exists()
    assert not (base / "sub/one.bin").exists()
    assert not (base / "sub/two.bin").exists()


def test_remote_item_exists_and_remove(client, config, monkeypatch_paramiko):
    # Upload once
    client.transfer_files(remove_on_success=False)
    base = PurePosixPath(config["remote"])
    fpath = base / "a.txt"
    assert client.remote_item_exists(fpath) is True

    # Remove file and prune empty parents (subdir parents remain because others exist)
    client.remove_remote_item(fpath)
    assert client.remote_item_exists(fpath) is False


def test_list_remote_items_nested(client, config, monkeypatch_paramiko):
    client.transfer_files(remove_on_success=False)
    base = PurePosixPath(config["remote"])
    root_items = set(client.list_remote_items(base))
    assert {"a.txt", "sub"} <= root_items

    sub_items = set(client.list_remote_items(base / "sub"))
    assert {"one.bin", "two.bin"} <= sub_items


# def test_setup_transfer_schedules_10min(client):
#     client.setup_transfer_schedules(interval=10)
#     # :00, :10, :20, :30, :40, :50
#     assert len(schedule.jobs) == 6
#     schedule.clear()


# def test_setup_transfer_schedules_every_2_hours(client):
#     client.setup_transfer_schedules(interval=120)  # every 2h
#     # 24 hours / 2 = 12 runs per day
#     assert len(schedule.jobs) == 12
#     schedule.clear()


# def test_setup_transfer_schedules_daily(client):
#     client.setup_transfer_schedules(interval=1440)  # daily
#     assert len(schedule.jobs) == 1
#     schedule.clear()
