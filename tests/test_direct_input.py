from yttool.search import extract_channel_url, extract_video_id


def test_video_urls():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s") == "dQw4w9WgXcQ"
    assert extract_video_id("youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_bare_video_ids():
    assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("a1_B2c3D4eF") == "a1_B2c3D4eF"
    # an 11-letter all-lowercase word is a search term, not an ID
    assert extract_video_id("engineering") is None
    assert extract_video_id("maintenance") is None
    assert extract_video_id("not a url at all") is None


def test_channel_forms():
    assert (
        extract_channel_url("UCddiUEpeqJcYeBxX1IVBKvQ")
        == "https://www.youtube.com/channel/UCddiUEpeqJcYeBxX1IVBKvQ"
    )
    assert extract_channel_url("@veritasium") == "https://www.youtube.com/@veritasium"
    assert extract_channel_url("https://www.youtube.com/@veritasium") == "https://www.youtube.com/@veritasium"
    assert extract_channel_url("https://www.youtube.com/@veritasium/videos") == "https://www.youtube.com/@veritasium"
    assert (
        extract_channel_url("https://www.youtube.com/channel/UCddiUEpeqJcYeBxX1IVBKvQ")
        == "https://www.youtube.com/channel/UCddiUEpeqJcYeBxX1IVBKvQ"
    )
    assert extract_channel_url("https://www.youtube.com/user/somename") == "https://www.youtube.com/user/somename"


def test_backfill_channel_name():
    from yttool.models import VideoMeta
    from yttool.search import backfill_channel_name

    metas = [VideoMeta(video_id="a", title="t"), VideoMeta(video_id="b", title="t", channel="Kept")]
    entries = [
        {"id": "a", "channel": None, "playlist_channel": "Example Channel", "playlist_uploader": "Example Channel"},
        {"id": "b"},
    ]
    backfill_channel_name(metas, entries)
    assert metas[0].channel == "Example Channel"
    assert metas[1].channel == "Kept"  # existing value not overwritten


def test_channel_is_not_video_and_vice_versa():
    assert extract_channel_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert extract_video_id("https://www.youtube.com/@veritasium") is None
    assert extract_channel_url("urban beekeeping basics") is None
