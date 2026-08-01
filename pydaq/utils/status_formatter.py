"""Compact, human-readable summaries of instrument records.

This module affects display only. The original records remain unchanged for
storage, transfer, dashboard access, and detailed file logging.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

_METADATA_KEYS = frozenset(
    {
        "dtm",
        "date",
        "time",
        "id",
        "record_id",
        "checksum",
        "Inst_SN",
        "row_id",
        "DateTime_1",
        "DateTime_2",
        "raw",
    }
)


def format_number(
    value: Any,
    *,
    decimal_places: int | None = None,
    significant_digits: int = 4,
) -> str:
    """Format one displayed number with a maximum significant-digit count.

    ``decimal_places`` preserves an existing operator-display convention when
    that representation already contains no more than ``significant_digits``.
    If it would expose too many significant digits, general formatting is used
    instead. The source value is never modified.

    Examples:
        ``38.8512`` -> ``38.85``
        ``5.8947`` -> ``5.895``
        ``462.5377`` with ``decimal_places=1`` -> ``462.5``

    Args:
        value: Numeric value or numeric string.
        decimal_places: Preferred fixed decimal places, or ``None``.
        significant_digits: Maximum number of significant digits.

    Returns:
        Compact number text, or ``-`` for a missing/non-finite value.
    """
    if value is None or value == "":
        return "-"
    if significant_digits < 1:
        raise ValueError("significant_digits must be at least 1")

    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not isfinite(number):
        return "-"

    # Avoid a visually confusing negative zero in compact status output.
    if number == 0:
        number = 0.0

    if decimal_places is not None:
        if decimal_places < 0:
            raise ValueError("decimal_places must be non-negative")
        fixed = f"{number:.{decimal_places}f}"
        if _significant_digit_count(fixed) <= significant_digits:
            return fixed

    return f"{number:.{significant_digits}g}"


def format_latest_record(record: Mapping[str, Any]) -> str:
    """Return a compact one-line representation of an instrument record.

    Known record shapes receive instrument-aware labels and units. Unknown
    shapes fall back to at most four scalar fields rather than the complete
    dictionary.

    Args:
        record: Latest instrument record.

    Returns:
        Compact status text without a sample-time prefix.
    """
    if not record:
        return "no data"

    # Thermo 49-series ozone analysers.
    ozone = _number(record, "o3")
    if ozone is not None:
        parts = [
            f"O3={format_number(ozone, decimal_places=1)} ppb"
        ]
        flags = _text(record.get("flags"))
        if flags:
            parts.append(f"flags={flags}")
        return " ".join(parts)

    # Vaisala HMP temperature / humidity probes.
    temperature = _number(record, "T")
    humidity = _number(record, "RH")
    if temperature is not None and humidity is not None:
        parts = [
            f"T={format_number(temperature, decimal_places=1)} °C",
            f"RH={format_number(humidity, decimal_places=1)} %",
        ]
        dewpoint = _number(record, "Td")
        if dewpoint is not None:
            parts.append(
                f"Td={format_number(dewpoint, decimal_places=1)} °C"
            )
        return " ".join(parts)

    # PALAS FIDAS: channels 61/62/64 are PM1/PM2.5/PM10 in mg/m³.
    # Only the displayed values are converted to µg/m³.
    pm1 = _number(record, "61", scale=1000.0)
    pm25 = _number(record, "62", scale=1000.0)
    pm10 = _number(record, "64", scale=1000.0)
    if any(value is not None for value in (pm1, pm25, pm10)):
        parts: list[str] = []
        if pm1 is not None:
            parts.append(
                f"PM1={format_number(pm1, decimal_places=1)}"
            )
        if pm25 is not None:
            parts.append(
                f"PM2.5={format_number(pm25, decimal_places=1)}"
            )
        if pm10 is not None:
            parts.append(
                f"PM10={format_number(pm10, decimal_places=1)}"
            )
        parts.append("µg/m³")
        return " ".join(parts)

    # Magee AE31/AE33: IR880/BC6 is the 880 nm black-carbon channel.
    bc880 = _number(record, "BC6")
    if bc880 is None:
        bc880 = _number(record, "IR880")
    if bc880 is not None:
        parts = [
            f"BC880={format_number(bc880, decimal_places=0)} ng/m³"
        ]

        # AE33 FlowC is reported in mL/min.
        flow_lpm = _number(record, "FlowC", scale=0.001)
        if flow_lpm is not None:
            parts.append(
                f"flow={format_number(flow_lpm, decimal_places=2)} L/min"
            )

        # The AE33 TCP Data-table record contains the instrument estimate of
        # remaining tape advances immediately after TapeAdvCount. The current
        # pydaq header calls this field ``unclear_3``.
        tape_remaining = _first_integer(
            record,
            (
                "TapeAdvRemaining",
                "TapeAdvanceLeft",
                "tape_advances_remaining",
                "unclear_3",
            ),
        )
        if tape_remaining is not None:
            tape_text = f"tape={tape_remaining} left"
            if tape_remaining < 10:
                tape_text += " CRITICAL"
            elif tape_remaining < 30:
                tape_text += " LOW"
            parts.append(tape_text)

        # In the current pydaq AE33 TCP header, ``Temp_3`` is actually the
        # overall AE33 Status field. It is a composite status code, not a
        # temperature. Prefer a correctly named field when one is available.
        status_code = _first_integer(
            record,
            ("Status", "status", "Temp_3"),
        )
        if status_code is not None:
            parts.append(_format_ae33_status(status_code))

        return " ".join(parts)

    # Ecotech/Acoem nephelometers using the ordinary instantaneous record.
    scattering = [_number(record, key) for key in ("ssp1", "ssp2", "ssp3")]
    if any(value is not None for value in scattering):
        parts = [
            f"ssp{index}={format_number(value, significant_digits=3)}"
            for index, value in enumerate(scattering, start=1)
            if value is not None
        ]
        rh = _number(record, "RH")
        if rh is not None:
            parts.append(
                f"RH={format_number(rh, decimal_places=1)} %"
            )
        return " ".join(parts)

    return _generic_summary(record)


def _format_ae33_status(status_code: int) -> str:
    """Decode the composite AE33 instrument status code."""
    if status_code == 0:
        return "status=0 (OK)"

    descriptions: list[str] = []

    operation = status_code & 0x0003
    descriptions.extend(
        {
            1: ["tape advance"],
            2: ["first measurement"],
            3: ["stopped"],
        }.get(operation, [])
    )

    flow = status_code & 0x000C
    descriptions.extend(
        {
            4: ["flow out of range"],
            8: ["flow-history warning"],
            12: ["flow out of range/history warning"],
        }.get(flow, [])
    )

    optical_source = status_code & 0x0030
    descriptions.extend(
        {
            16: ["LED calibration"],
            32: ["LED warning"],
            48: ["LED error"],
        }.get(optical_source, [])
    )
    if status_code & 0x0040:
        descriptions.append("chamber error")

    filter_tape = status_code & 0x0180
    descriptions.extend(
        {
            128: ["tape low"],
            256: ["tape nearly exhausted"],
            384: ["tape error/end"],
        }.get(filter_tape, [])
    )

    if status_code & 0x0200:
        descriptions.append("setup warning")

    test_code = status_code & 0x1C00
    descriptions.extend(
        {
            1024: ["stability test"],
            2048: ["clean-air test"],
            3072: ["change-tape procedure"],
            4096: ["optical test"],
            6144: ["leakage test"],
        }.get(test_code, [f"test code {test_code}"] if test_code else [])
    )

    if status_code & 0x2000:
        descriptions.append("external-device error")
    if status_code & 0x4000:
        descriptions.append("clean-air test failed")
    if status_code & 0x8000:
        descriptions.append("CF-card error")
    if status_code & 0x10000:
        descriptions.append("database-limit warning")

    decoded_mask = (
        0x0003
        | 0x000C
        | 0x0030
        | 0x0040
        | 0x0180
        | 0x0200
        | 0x1C00
        | 0x2000
        | 0x4000
        | 0x8000
        | 0x10000
    )
    unknown_bits = status_code & ~decoded_mask
    if unknown_bits:
        descriptions.append(f"unknown bits {unknown_bits}")

    detail = ", ".join(descriptions) if descriptions else "unclassified"
    return f"status={status_code} ({detail})"


def _number(
    record: Mapping[str, Any],
    key: str,
    *,
    scale: float = 1.0,
) -> float | None:
    value = record.get(key)
    if value is None or value == "":
        return None
    try:
        number = float(value) * scale
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _integer(record: Mapping[str, Any], key: str) -> int | None:
    value = _number(record, key)
    if value is None:
        return None
    rounded = round(value)
    if abs(value - rounded) > 1e-6:
        return None
    return int(rounded)


def _first_integer(
    record: Mapping[str, Any],
    keys: tuple[str, ...],
) -> int | None:
    for key in keys:
        value = _integer(record, key)
        if value is not None:
            return value
    return None


def _text(value: Any, *, max_length: int = 40) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}…"


def _generic_summary(
    record: Mapping[str, Any],
    *,
    max_fields: int = 4,
) -> str:
    parts: list[str] = []
    for key, value in record.items():
        if key in _METADATA_KEYS or key.startswith("unclear"):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue

        if isinstance(value, bool):
            text = str(value)
        elif isinstance(value, (int, float)):
            text = format_number(value)
            if text == "-":
                continue
        else:
            text = _text(value, max_length=24)
        if not text:
            continue

        parts.append(f"{key}={text}")
        if len(parts) >= max_fields:
            break

    return " ".join(parts) if parts else f"{len(record)} fields"


def _significant_digit_count(text: str) -> int:
    """Count significant digits in fixed-format numeric text."""
    mantissa = text.lower().split("e", 1)[0].lstrip("+-")
    digits = "".join(character for character in mantissa if character.isdigit())
    significant = digits.lstrip("0")
    return len(significant) if significant else 1
