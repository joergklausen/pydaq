from __future__ import annotations

import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from pydaq.instruments.tahmo import ZentraV4Client, write_batch_zip


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(
            f"Set {name} to run the live TAHMO/ZENTRA Cloud integration test."
        )
    return value


def test_tahmo_short_live_download(tmp_path: Path) -> None:
    """Download a deliberately short real-data interval and save it as ZIP/CSV."""
    device_sn = _required_env("TAHMO_DEVICE_SN")
    start = _required_env("TAHMO_START")
    end = _required_env("TAHMO_END")
    token_file = Path(
        os.environ.get("TAHMO_TOKEN_FILE", "~/.secrets/zentra-api-token")
    ).expanduser()

    assert token_file.exists(), f"Token file does not exist: {token_file}"

    client = ZentraV4Client(
        server=os.environ.get(
            "TAHMO_SERVER",
            "https://tahmo.zentracloud.com",
        ),
        token_file=token_file,
        timeout_seconds=30,
    )
    batch = client.fetch_readings(
        device_sn=device_sn,
        start_date=start,
        end_date=end,
        per_page=2000,
    )

    assert batch.rows, (
        f"No readings returned for device={device_sn!r}, "
        f"start={start!r}, end={end!r}"
    )
    assert batch.last_mrid is not None

    target = tmp_path / f"tahmo-{device_sn}-probe.zip"
    write_batch_zip(batch=batch, path=target)

    assert target.exists()
    with ZipFile(target) as archive:
        names = archive.namelist()
        assert len(names) == 1
        csv_text = archive.read(names[0]).decode("utf-8")

    assert "measurement" in csv_text
    assert "mrid" in csv_text
    print(f"TAHMO probe: rows={len(batch.rows)} last_mrid={batch.last_mrid}")
    print(f"TAHMO probe ZIP: {target}")
