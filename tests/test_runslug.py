import json
from pathlib import Path

from yttool.runslug import (
    channel_slug,
    rename_runs,
    slug_from_run_meta,
    slugify,
    videos_slug,
)


def test_slugify_keeps_non_latin_scripts():
    assert slugify("Градинарство за начинаещи в София") == "градинарство-за-начинаещи-в-софия"
    assert slugify("Hello, World!  Again") == "hello-world-again"
    assert slugify("!!!") == "query"


def test_slugify_normalizes_decomposed_accents():
    assert slugify("Cafe\u0301 Pastry Basics") == "café-pastry-basics"


def test_slugify_truncates_on_word_boundary():
    out = slugify("urban beekeeping basics for small city gardens", max_len=40)
    assert out == "urban-beekeeping-basics-for-small-city"
    assert len(out) <= 40


def test_single_video_uses_title():
    assert videos_slug(["Why My Sourdough Starter Didn't Rise This Winter"]) == (
        "video-why-my-sourdough-starter-didnt-rise-this"
    )


def test_series_collapses_common_prefix():
    slug = videos_slug([
        "Beekeeping for Beginners Part 1",
        "Beekeeping for Beginners - Part 2: Spring Inspections of Hive Boxes",
    ])
    assert slug == "videos-beekeeping-for-beginners-part-1+2-spring-inspections-of"


def test_unrelated_videos_join_with_plus_and_count():
    slug = videos_slug(["Alpha bravo charlie delta echo", "Foxtrot golf hotel", "India", "Juliet", "Kilo"])
    assert slug == "videos-alpha-bravo-charlie+foxtrot-golf-hotel+india+2-more"


def test_channel_slug_falls_back_to_handle():
    assert channel_slug("Example Channel") == "channel-example-channel"
    assert channel_slug("", fallback="https://www.youtube.com/@ExampleChannel") == "channel-examplechannel"


def test_slug_from_run_meta_by_mode():
    direct = {"query": "T", "videos": [{"title": "Some Talk", "found_by": "direct"}]}
    assert slug_from_run_meta(direct) == "video-some-talk"
    channel = {"query": "latest videos from https://www.youtube.com/@Foo",
               "videos": [{"title": "x", "channel": "", "found_by": "channel"}]}
    assert slug_from_run_meta(channel) == "channel-foo"
    topic = {"query": "growing tomatoes on a balcony", "videos": [{"title": "x", "found_by": "balcony tomatoes"}]}
    assert slug_from_run_meta(topic) == "growing-tomatoes-on-a-balcony"


def test_rename_runs_renames_only_changed_and_skips_collisions(tmp_path: Path):
    def mk(name, meta):
        d = tmp_path / name
        d.mkdir()
        (d / "run.json").write_text(json.dumps(meta), encoding="utf-8")
        return d

    mk("videos-abc123-20260101-010101", {"query": "q", "videos": [{"title": "My Talk", "found_by": "direct"}]})
    mk("growing-tomatoes-on-a-balcony-20260101-020202", {"query": "growing tomatoes on a balcony", "videos": []})
    mk("videos-def456-20260101-030303", {"query": "q", "videos": [{"title": "Taken", "found_by": "direct"}]})
    mk("video-taken-20260101-030303", {"query": "q", "videos": [{"title": "Taken", "found_by": "direct"}]})
    (tmp_path / "not-a-run").mkdir()

    dry = rename_runs(tmp_path, dry_run=True)
    assert [(o.name, n.name) for o, n in dry] == [("videos-abc123-20260101-010101", "video-my-talk-20260101-010101")]
    assert (tmp_path / "videos-abc123-20260101-010101").exists()

    done = rename_runs(tmp_path)
    assert len(done) == 1
    assert (tmp_path / "video-my-talk-20260101-010101").is_dir()
    assert (tmp_path / "videos-def456-20260101-030303").is_dir()  # collision skipped
    assert rename_runs(tmp_path) == []
