"""Tests for the shared JSON-from-LLM extraction helpers."""

from __future__ import annotations

import pytest

from agents._json import extract_json_blob, parse_json_list, parse_json_object

# --------------------------------------------------------------------------- #
# extract_json_blob                                                           #
# --------------------------------------------------------------------------- #


def test_extract_blob_returns_none_for_empty() -> None:
    assert extract_json_blob("") is None
    assert extract_json_blob("   \n   ") is None


def test_extract_blob_pulls_json_fence() -> None:
    text = 'prose\n```json\n{"a": 1}\n```\nmore prose'
    assert extract_json_blob(text) == '{"a": 1}'


def test_extract_blob_pulls_plain_fence() -> None:
    text = 'prose\n```\n[1, 2, 3]\n```'
    assert extract_json_blob(text) == "[1, 2, 3]"


def test_extract_blob_picks_earliest_of_bracket_or_brace() -> None:
    # `[` appears before `{` -> take the array.
    assert extract_json_blob('prose [1] then {"a":1}') == '[1] then {"a":1}'
    # `{` appears before `[` -> take the object slice.
    assert extract_json_blob('prose {"a":[1]} then [2]') == '{"a":[1]} then [2]'


def test_extract_blob_none_when_no_json() -> None:
    assert extract_json_blob("plain prose, no json") is None


# --------------------------------------------------------------------------- #
# parse_json_list                                                             #
# --------------------------------------------------------------------------- #


def test_parse_list_returns_array() -> None:
    assert parse_json_list('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]


def test_parse_list_wraps_single_object() -> None:
    assert parse_json_list('{"a": 1}') == [{"a": 1}]


def test_parse_list_drops_non_dict_items() -> None:
    assert parse_json_list('[{"a": 1}, "string", 42, {"b": 2}]') == [{"a": 1}, {"b": 2}]


def test_parse_list_empty_on_garbage() -> None:
    assert parse_json_list("not json at all") == []
    assert parse_json_list("{malformed") == []


def test_parse_list_strips_fence() -> None:
    text = '```json\n[{"k": "v"}]\n```'
    assert parse_json_list(text) == [{"k": "v"}]


# --------------------------------------------------------------------------- #
# parse_json_object                                                           #
# --------------------------------------------------------------------------- #


def test_parse_object_returns_dict() -> None:
    assert parse_json_object('{"x": 1, "y": [2, 3]}') == {"x": 1, "y": [2, 3]}


def test_parse_object_returns_none_for_top_level_array() -> None:
    assert parse_json_object('[1, 2, 3]') is None


@pytest.mark.parametrize("bad", ["", "   ", "no json", "{malformed", "42"])
def test_parse_object_returns_none_for_unparseable(bad: str) -> None:
    assert parse_json_object(bad) is None
