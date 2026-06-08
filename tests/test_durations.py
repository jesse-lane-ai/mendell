"""Unit tests for duration parsing utilities."""

import pytest
from mendell.durations import parse_duration_ms, format_duration_ms
from mendell.errors import BadInputError


def test_parse_duration_ms_valid():
    assert parse_duration_ms("100ms") == 100.0
    assert parse_duration_ms("1.5s") == 1500.0
    assert parse_duration_ms(250) == 250.0
    assert parse_duration_ms("0ms") == 0.0
    assert parse_duration_ms("-50ms") == -50.0


def test_parse_duration_ms_invalid():
    with pytest.raises(BadInputError):
        parse_duration_ms("invalid")
    with pytest.raises(BadInputError):
        parse_duration_ms("100")


def test_format_duration_ms():
    assert format_duration_ms(1000) == "1s"
    assert format_duration_ms(100) == "100ms"
    assert format_duration_ms(1500) == "1500ms"  # not a whole second -> ms
    assert format_duration_ms(2500) == "2500ms"


def test_roundtrip():
    for val in ["50ms", "2s", "1000ms"]:
        ms = parse_duration_ms(val)
        formatted = format_duration_ms(ms)
        assert parse_duration_ms(formatted) == ms
