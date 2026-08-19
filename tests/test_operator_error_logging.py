from __future__ import annotations

import io
import logging

from pydaq.instruments.instrument import Instrument
from pydaq.utils.logging_handler import CompactConsoleHandler


def _test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


def test_compact_console_suppresses_traceback_but_file_handler_keeps_it() -> None:
    logger = _test_logger("test.operator.compact")
    console_stream = io.StringIO()
    file_stream = io.StringIO()
    formatter = logging.Formatter("%(levelname)s %(message)s")

    console = CompactConsoleHandler(console_stream)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    file_handler = logging.StreamHandler(file_stream)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)

    try:
        raise TimeoutError("timed out")
    except TimeoutError:
        logger.error(
            "[ne300] unavailable: tcp 192.168.3.149:32783 timed out during initialization",
            exc_info=True,
            extra={"console_compact": True},
        )

    console_text = console_stream.getvalue()
    file_text = file_stream.getvalue()
    assert "unavailable: tcp 192.168.3.149:32783" in console_text
    assert "Traceback" not in console_text
    assert "TimeoutError: timed out" not in console_text
    assert "Traceback" in file_text
    assert "TimeoutError: timed out" in file_text


def test_timeout_is_rendered_as_operator_availability_message(tmp_path) -> None:
    logger = _test_logger("test.operator.instrument")
    logger.addHandler(logging.NullHandler())
    instrument = Instrument(
        name="ne300",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logger,
        headers=None,
        parameters={
            "io": {
                "kind": "tcp",
                "host": "192.168.3.149",
                "port": 32783,
            }
        },
    )

    message = instrument._operator_error(
        instrument.initialize,
        TimeoutError("timed out"),
    )
    assert message == (
        "unavailable: tcp 192.168.3.149:32783 "
        "timed out during initialization"
    )


def test_generic_exception_is_not_misreported_as_instrument_unavailable(tmp_path) -> None:
    logger = _test_logger("test.operator.software")
    logger.addHandler(logging.NullHandler())
    instrument = Instrument(
        name="ne300",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logger,
        headers=None,
        parameters={"io": {"kind": "tcp", "host": "192.168.3.149", "port": 32783}},
    )

    message = instrument._operator_error(
        instrument.initialize,
        ValueError("invalid packet header"),
    )
    assert message == (
        "software/protocol error during initialization: "
        "ValueError: invalid packet header"
    )
