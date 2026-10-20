"""Exact Premiere tick/frame and timecode helpers.

Premiere's ticks are stored as decimal strings at the boundary. Fraction is used
internally so fractional frame rates never pass through binary floating point.
"""

from __future__ import annotations

import re
from fractions import Fraction

TICKS_PER_SECOND = 254_016_000_000
_TIME_RE = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})[:;](\d{2})$")


def parse_rate(value: str | int | float | tuple[int, int] | Fraction) -> Fraction:
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, tuple):
        rate = Fraction(value[0], value[1])
    elif isinstance(value, float):
        # Decimal text gives stable common NTSC rate recognition.
        rate = Fraction(str(value)).limit_denominator(100_000)
    elif isinstance(value, int):
        rate = Fraction(value, 1)
    else:
        text = value.strip()
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            rate = Fraction(int(numerator), int(denominator))
        else:
            rate = Fraction(text).limit_denominator(100_000)
    if rate <= 0:
        raise ValueError("frame rate must be positive")
    return rate


def ticks_per_frame(rate: str | int | float | tuple[int, int] | Fraction) -> Fraction:
    parsed = parse_rate(rate)
    return Fraction(TICKS_PER_SECOND * parsed.denominator, parsed.numerator)


def frame_to_ticks(frame: int, rate: str | int | float | tuple[int, int] | Fraction) -> int:
    if frame < 0:
        raise ValueError("negative sequence frames are not supported")
    value = Fraction(frame) * ticks_per_frame(rate)
    if value.denominator != 1:
        # Premiere ticks can represent all supported integer frame boundaries;
        # retain deterministic nearest-tick conversion for unusual timebases.
        return round_half_up(value)
    return value.numerator


def ticks_to_frame(ticks: str | int, rate: str | int | float | tuple[int, int] | Fraction) -> int:
    value = Fraction(int(ticks), 1) / ticks_per_frame(rate)
    return round_half_up(value)


def frame_boundary_ticks(ticks: str | int, rate: str | int | float | tuple[int, int] | Fraction) -> bool:
    value = Fraction(int(ticks), 1) / ticks_per_frame(rate)
    return value.denominator == 1


def normalize_interval(start_ticks: str | int, end_ticks: str | int) -> dict[str, str | int | bool]:
    start = int(start_ticks)
    end = int(end_ticks)
    if start == end:
        return {
            "start_ticks": str(start),
            "end_ticks": str(end),
            "direction": "stationary",
            "valid": False,
            "reason": "zero_length",
        }
    reverse = start > end
    return {
        "start_ticks": str(min(start, end)),
        "end_ticks": str(max(start, end)),
        "direction": "reverse" if reverse else "forward",
        "valid": True,
        "end_exclusive": True,
    }


def parse_timecode(text: str, rate: str | int | float | tuple[int, int] | Fraction) -> int:
    match = _TIME_RE.fullmatch(text.strip())
    if not match:
        raise ValueError("timecode must be HH:MM:SS:FF or HH:MM:SS;FF")
    hours, minutes, seconds, frames = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("minutes and seconds must be less than 60")
    parsed_rate = parse_rate(rate)
    nominal = round(float(parsed_rate))
    if frames >= nominal:
        raise ValueError("frame field exceeds nominal frame rate")
    total_minutes = hours * 60 + minutes
    count = ((hours * 3600 + minutes * 60 + seconds) * nominal) + frames
    drop = drop_count(parsed_rate)
    if drop and minutes % 10 != 0 and seconds == 0 and frames < drop:
        raise ValueError("timecode names a frame omitted by drop-frame numbering")
    if drop:
        count -= drop * (total_minutes - total_minutes // 10)
    return frame_to_ticks(count, parsed_rate)


def format_timecode(ticks: str | int, rate: str | int | float | tuple[int, int] | Fraction, drop_frame: bool = False) -> str:
    parsed_rate = parse_rate(rate)
    nominal = round(float(parsed_rate))
    count = ticks_to_frame(ticks, parsed_rate)
    sep = ";" if drop_frame else ":"
    drop = drop_count(parsed_rate) if drop_frame else 0
    if drop:
        per_ten = nominal * 60 * 10 - drop * 9
        per_minute = nominal * 60 - drop
        ten_minute_blocks, remainder = divmod(count, per_ten)
        extra_minutes = 0 if remainder < nominal * 60 else (remainder - nominal * 60) // per_minute + 1
        count += drop * (9 * ten_minute_blocks + extra_minutes)
    hours, remainder = divmod(count, nominal * 3600)
    minutes, remainder = divmod(remainder, nominal * 60)
    seconds, frames = divmod(remainder, nominal)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frames:02d}"


def drop_count(rate: Fraction) -> int:
    if rate == Fraction(30_000, 1_001):
        return 2
    if rate == Fraction(60_000, 1_001):
        return 4
    return 0


def round_half_up(value: Fraction) -> int:
    if value < 0:
        return -round_half_up(-value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)

