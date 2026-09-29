"""JSON data cleaning must not change names, labels, or finite numbers."""

import asyncio
import json
import time
import tracemalloc
from unittest.mock import AsyncMock

import pytest

from ptc_agent.agent.tools.show_widget import _resolve_data_files


@pytest.mark.parametrize("extension", ["json", "geojson", "topojson", "JSON"])
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            '{"label":"Infinity Growth","literal":"NaN","negative":"-Infinity"}',
            '{"label":"Infinity Growth","literal":"NaN","negative":"-Infinity"}',
            id="quoted-values",
        ),
        pytest.param(
            '{"NaN":1,"Infinity":2,"-Infinity":3}',
            '{"NaN":1,"Infinity":2,"-Infinity":3}',
            id="quoted-keys",
        ),
        pytest.param(
            json.dumps({"label": 'say "NaN" and "Infinity"', "path": r"C:\Infinity"}),
            json.dumps({"label": 'say "NaN" and "Infinity"', "path": r"C:\Infinity"}),
            id="escaped-quotes-and-backslash",
        ),
        pytest.param(
            '{"nested":{"label":"Revenue","values":[1.2300,2e+02]},'
            '"ids":[9007199254740993],"value":null}',
            '{"nested":{"label":"Revenue","values":[1.2300,2e+02]},'
            '"ids":[9007199254740993],"value":null}',
            id="normal-json-keeps-format-and-precision",
        ),
        pytest.param(
            '[NaN, Infinity, -Infinity]',
            '[null, null, null]',
            id="nonstandard-numeric-constants",
        ),
        pytest.param(
            '{"value":NaN,"nested":[Infinity,-Infinity,{"label":"NaN"}]}',
            '{"value":null,"nested":[null,null,{"label":"NaN"}]}',
            id="mixed-nested-data",
        ),
    ],
)
def test_json_data_preserves_strings_while_cleaning_constants(extension, payload, expected):
    """For every JSON-family extension, quoted text survives untouched and
    only bare NaN/Infinity constants become null."""
    backend = AsyncMock()
    backend.aread_text.return_value = payload
    filename = f"chart.{extension}"

    result = asyncio.run(_resolve_data_files(backend, [f"/work/{filename}"]))

    assert result == {filename: expected}
    json.loads(result[filename])


def test_csv_data_is_not_cleaned_as_json():
    """CSV text is returned byte-for-byte: sanitizing is limited to the
    JSON-family extensions."""
    backend = AsyncMock()
    payload = "label,value\nInfinity Growth,NaN\n"
    backend.aread_text.return_value = payload

    result = asyncio.run(_resolve_data_files(backend, ["/work/chart.csv"]))

    assert result == {"chart.csv": payload}


def test_truncated_json_with_escaped_quotes_is_intact_and_fast():
    """A file cut off inside a string full of escaped quotes is the worst case
    for a string-first scan that can backtrack: it used to rescan to the end
    from every quote (O(n^2); measured ~108 s at this size before the fix). The
    tail is one unterminated string, so nothing may be rewritten, and the call
    must stay linear."""
    payload = '{"rows":[{"html":"' + '\\"' * 100_000
    backend = AsyncMock()
    backend.aread_text.return_value = payload
    filename = "truncated.json"

    started = time.perf_counter()
    result = asyncio.run(_resolve_data_files(backend, [f"/work/{filename}"]))
    elapsed = time.perf_counter() - started

    assert result == {filename: payload}
    # Generous quadratic-scan detector, not a microbenchmark: the fixed scan
    # takes well under 10ms here.
    assert elapsed < 5.0


def test_large_single_string_is_preserved_while_bare_nan_becomes_null():
    """A 450,000-character string plus quoted NaN/Infinity tokens must come
    back byte-for-byte while the adjacent bare NaN is rewritten to null in
    the same sanitizer pass."""
    blob = "A" * 450_000
    payload = f'{{"blob":"{blob}","label":"NaN Infinity -Infinity","v":NaN,"scale":1.2300e+02}}'
    expected = f'{{"blob":"{blob}","label":"NaN Infinity -Infinity","v":null,"scale":1.2300e+02}}'
    backend = AsyncMock()
    backend.aread_text.return_value = payload
    filename = "large.json"

    # Sized to fit under the real 500 KB inline cap, so no test-local cap
    # patch is needed to get the sanitized value back.
    result = asyncio.run(_resolve_data_files(backend, [f"/work/{filename}"]))

    assert result == {filename: expected}
    parsed = json.loads(result[filename])
    assert parsed["blob"] == blob
    assert parsed["label"] == "NaN Infinity -Infinity"
    assert parsed["v"] is None
    assert parsed["scale"] == 123


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("A" * 4_000_000, id="plain-text"),
        pytest.param('\\"' * 2_000_000, id="escaped-quote-pairs"),
    ],
)
def test_multi_mb_single_string_uses_bounded_memory(body):
    """The sanitizer runs before the 500 KB inline cap, so a multi-MB file
    holding one huge string must not cost more than a few copies of itself.
    A repeated group in a regex keeps per-iteration backtrack state (about
    120 bytes per character), so the peak here was ~480 MB for 4 MB of input
    before the string branch became possessive. The peak is measured with
    tracemalloc rather than wall-clock time, and the bound sits an order of
    magnitude under the old behaviour and well above the linear cost."""
    payload = f'{{"blob":"{body}","v":NaN}}'
    backend = AsyncMock()
    backend.aread_text.return_value = payload

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = asyncio.run(_resolve_data_files(backend, ["/work/huge.json"]))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Over the inline cap: dropped, exactly as before the sanitizer changed.
    assert result == {}
    assert peak < 64 * 1024 * 1024, f"sanitizer peak {peak / 1e6:.0f} MB"
