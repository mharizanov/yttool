from yttool.search import parse_search_entries


def entry(vid, **kw):
    return {"id": vid, "title": f"title {vid}", "channel": "chan", "duration": 100, "view_count": 5, **kw}


def test_filters_live_and_upcoming():
    entries = [
        entry("a"),
        entry("b", live_status="is_live"),
        entry("c", live_status="is_upcoming"),
        entry("d", live_status="was_live"),
        entry("e"),
    ]
    metas = parse_search_entries(entries, top=10)
    assert [m.video_id for m in metas] == ["a", "d", "e"]


def test_filters_members_only():
    entries = [entry("a", availability="subscriber_only"), entry("b")]
    metas = parse_search_entries(entries, top=10)
    assert [m.video_id for m in metas] == ["b"]


def test_respects_top():
    entries = [entry(str(i)) for i in range(20)]
    assert len(parse_search_entries(entries, top=7)) == 7


def test_skips_missing_id():
    assert parse_search_entries([{"title": "no id"}, entry("x")], top=5)[0].video_id == "x"
