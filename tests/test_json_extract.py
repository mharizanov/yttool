import pytest

from yttool.llm import extract_json_object


def test_plain_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_in_prose():
    text = 'Here is the result: {"a": {"b": "with } brace in string"}} and trailing words'
    assert extract_json_object(text) == {"a": {"b": "with } brace in string"}}


def test_no_json_raises():
    with pytest.raises(ValueError):
        extract_json_object("no json here at all")


def test_truncated_json_raises():
    with pytest.raises((ValueError, Exception)):
        extract_json_object('{"a": {"b": 1')
