import time

from yttool.cache import Cache, stable_hash


def test_stable_hash_is_stable_and_distinct():
    assert stable_hash("a", 1, "en") == stable_hash("a", 1, "en")
    assert stable_hash("a", 1, "en") != stable_hash("a", 2, "en")
    assert len(stable_hash("x")) == 16


def test_put_get_roundtrip(tmp_path):
    cache = Cache(tmp_path)
    cache.put("sub/dir/item.json", {"k": [1, 2]})
    assert cache.get("sub/dir/item.json") == {"k": [1, 2]}


def test_ttl_expiry(tmp_path):
    cache = Cache(tmp_path)
    cache.put("item.json", {"k": 1})
    assert cache.get("item.json", ttl_seconds=60) == {"k": 1}
    old = time.time() - 3600
    import os

    os.utime(cache.path("item.json"), (old, old))
    assert cache.get("item.json", ttl_seconds=60) is None


def test_corrupt_file_returns_none(tmp_path):
    cache = Cache(tmp_path)
    path = cache.path("bad.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert cache.get("bad.json") is None
