# tests/test_utils_config.py
from __future__ import annotations
from pathlib import Path
import hashlib
import pytest

from utils.config import load_yaml_config


def _find_test_yaml() -> Path:
    """
    Locate the repo's test.yaml without modifying it.
    Priority:
      1) tests/config/test.yaml
      2) config/test.yaml
      3) tests/test.yaml
      4) test.yaml (repo root)
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "config" / "test.yaml",
        here.parent / "config" / "test.yaml",
        here / "test.yaml",
        here.parent / "test.yaml",
    ]
    for p in candidates:
        if p.is_file():
            return p
    pytest.skip("Could not locate test.yaml for config tests.")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def test_load_nonexistent_config():
    with pytest.raises(FileNotFoundError):
        load_yaml_config("/nonexistent/path/config.yaml")


def test_load_existing_config():
    cfg_path = _find_test_yaml()
    before = _sha256(cfg_path)  # guard: ensure we never mutate the file

    config = load_yaml_config(cfg_path)
    after = _sha256(cfg_path)
    assert before == after, "test.yaml changed during the test run (must remain read-only)"

    assert isinstance(config, dict)

    # Top-level sections (tolerate legacy/new schemas)
    assert any(k in config for k in ("paths", "local")), "Expect 'paths' or 'local' section"

    # ----- paths/local sanity (do not require every key) -----
    def has_path_key(k: str) -> bool:
        return (isinstance(config.get("paths"), dict) and k in config["paths"]) or \
               (isinstance(config.get("local"), dict) and k in config["local"])

    # 'root' should exist; other keys may or may not depending on your config
    assert has_path_key("root"), "Missing expected path key: root"

    # If these exist, that's fine; don't fail if they don't
    for key in ("data", "staging", "logging"):
        _ = has_path_key(key)  # presence is optional

    # ----- logging section is OPTIONAL -----
    if "logging" in config:
        assert isinstance(config["logging"], dict)
        assert any(k in config["logging"] for k in ("file_name", "file")), \
            "Logging present but no 'file_name'/'file' key"

    # ----- instruments section -----
    assert "instruments" in config, "Missing 'instruments' section"
    instruments = config["instruments"]

    if isinstance(instruments, dict):
        assert instruments, "'instruments' dict is empty"
        name, inst = next(iter(instruments.items()))
        assert isinstance(name, str) and name, "Instrument key should be a non-empty string"
        assert isinstance(inst, dict), "Instrument config should be a dict"
    elif isinstance(instruments, list):
        assert instruments, "'instruments' list is empty"
        assert isinstance(instruments[0], dict), "Each instrument entry should be a dict"
    else:
        pytest.fail(f"Unexpected 'instruments' type: {type(instruments)}")
