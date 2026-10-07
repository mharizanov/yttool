import pytest

from yttool.cli import parse_duration_arg
from yttool.search import (
    SearchFilters,
    _search_target,
    compose_sp,
    parse_search_entries,
)


def test_compose_sp_known_good_codes():
    assert compose_sp(since_bucket="week") == "EgIIAw=="
    assert compose_sp(min_seconds=1200) == "EgIYAg=="
    assert compose_sp(since_bucket="week", min_seconds=1500) == "EgQIAxgC"
    assert compose_sp(max_seconds=240) == "EgIYAQ=="
    assert compose_sp() is None
    assert compose_sp(min_seconds=300) is None  # 5min doesn't reach the 'long' bucket


def test_parse_duration_arg():
    assert parse_duration_arg("90s") == 90
    assert parse_duration_arg("10m") == 600
    assert parse_duration_arg("1h30m") == 5400
    assert parse_duration_arg("15") == 900  # bare number = minutes
    with pytest.raises(SystemExit):
        parse_duration_arg("banana")


def test_search_target_forms():
    plain = _search_target("beekeeping", 10, None)
    assert plain == ["ytsearch10:beekeeping"]

    channel = _search_target("hive care", 10, SearchFilters(channel_url="https://www.youtube.com/@x"))
    assert channel[0] == "https://www.youtube.com/@x/search?query=hive%20care"
    assert channel[1:] == ["--playlist-items", "1:10"]

    filtered = _search_target("hives", 8, SearchFilters(since_bucket="year", min_seconds=1200))
    assert filtered[0].startswith("https://www.youtube.com/results?search_query=hives&sp=")
    assert "EgQIBRgC" in filtered[0]  # year(5) + long(2)


def test_duration_post_filter():
    entries = [
        {"id": "a", "title": "short", "duration": 100},
        {"id": "b", "title": "mid", "duration": 900},
        {"id": "c", "title": "long", "duration": 4000},
        {"id": "d", "title": "unknown", "duration": None},
    ]
    filters = SearchFilters(min_seconds=600, max_seconds=3600)
    metas = parse_search_entries(entries, filters=filters)
    assert [m.video_id for m in metas] == ["b", "d"]  # unknowns pass


def test_filters_change_cache_repr():
    a = SearchFilters(channel_url="x", min_seconds=60)
    b = SearchFilters(channel_url="x", min_seconds=120)
    c = SearchFilters(channel_url="x", min_seconds=60)
    assert a.cache_repr() != b.cache_repr()
    assert a.cache_repr() == c.cache_repr()
    assert not SearchFilters().active
    assert SearchFilters(since_bucket="day").active
