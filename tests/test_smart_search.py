import yttool.search as search_mod
from yttool.cache import Cache
from yttool.llm import LlmConfig
from yttool.prompts_io import load_prompts
from yttool.search import gather_candidates, select_videos, smart_search

CFG = LlmConfig(provider="test", model="test-model", api_key="x")

FAKE_RESULTS = {
    "sensor networks": [
        {"id": "sn1", "title": "Sensor Net Intro", "channel": "A", "duration": 600, "view_count": 100},
        {"id": "sn2", "title": "Sensor Net Expo", "channel": "B", "duration": 300, "view_count": 200},
        {"id": "shared", "title": "Beekeeping with sensors", "channel": "C", "duration": 900, "view_count": 50},
    ],
    "urban beekeeping": [
        {"id": "bk1", "title": "Beekeeping 101", "channel": "D", "duration": 3600, "view_count": 500},
        {"id": "shared", "title": "Beekeeping with sensors", "channel": "C", "duration": 900, "view_count": 50},
    ],
    "empty query": [],
}


def fake_run_ytsearch(query, count, filters=None):
    return list(FAKE_RESULTS.get(query, []))


def make_fake_llm(plan_queries, selected_ids):
    def fake_call_llm_json(client, config, system_prompt, user_prompt, label, **kwargs):
        if label == "query plan":
            return {"queries": [{"query": q, "angle": "test"} for q in plan_queries]}
        if label == "video selection":
            return {"selected": [{"video_id": vid, "reason": f"picked {vid}"} for vid in selected_ids]}
        raise AssertionError(f"unexpected label {label}")

    return fake_call_llm_json


def test_gather_candidates_dedupes_across_queries(monkeypatch):
    monkeypatch.setattr(search_mod, "_run_ytsearch", fake_run_ytsearch)
    candidates = gather_candidates(["sensor networks", "urban beekeeping", "empty query"], 10)
    ids = [m.video_id for m in candidates]
    assert ids == ["sn1", "sn2", "shared", "bk1"]
    assert candidates[2].found_by == "sensor networks"  # first query wins


def test_select_videos_passthrough_when_few(monkeypatch):
    monkeypatch.setattr(search_mod, "_run_ytsearch", fake_run_ytsearch)
    candidates = gather_candidates(["urban beekeeping"], 10)
    # 2 candidates <= top 5: no LLM call needed
    assert select_videos("t", candidates, 5, None, CFG, {}, "auto") == candidates


def test_smart_search_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(search_mod, "_run_ytsearch", fake_run_ytsearch)
    monkeypatch.setattr(
        search_mod, "call_llm_json",
        make_fake_llm(["sensor networks", "urban beekeeping"], ["bk1", "sn1", "bogus_id"]),
        raising=False,
    )
    # patch the lazy import inside the functions
    import yttool.llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "call_llm_json",
        make_fake_llm(["sensor networks", "urban beekeeping"], ["bk1", "sn1", "bogus_id"]),
    )
    prompts, _ = load_prompts()
    cache = Cache(tmp_path)

    selected = smart_search("compound topic", 2, "en", cache, None, CFG, prompts)
    assert [m.video_id for m in selected] == ["bk1", "sn1"]  # ranked, bogus id dropped
    assert selected[0].why == "picked bk1"

    # second call must hit the plan cache and do no searching or LLM work
    def boom(*a, **k):
        raise AssertionError("should have been cached")

    monkeypatch.setattr(search_mod, "_run_ytsearch", boom)
    monkeypatch.setattr(llm_mod, "call_llm_json", boom)
    cached = smart_search("compound topic", 2, "en", cache, None, CFG, prompts)
    assert [m.video_id for m in cached] == ["bk1", "sn1"]
    assert cached[0].why == "picked bk1"


def test_smart_search_falls_back_when_planning_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(search_mod, "_run_ytsearch", fake_run_ytsearch)
    import yttool.llm as llm_mod

    def failing_plan(client, config, system_prompt, user_prompt, label, **kwargs):
        if label == "query plan":
            raise RuntimeError("LLM down")
        return {"selected": []}

    monkeypatch.setattr(llm_mod, "call_llm_json", failing_plan)
    prompts, _ = load_prompts()
    selected = smart_search("urban beekeeping", 5, "en", Cache(tmp_path), None, CFG, prompts)
    # falls back to the raw topic as the only query, selection passthrough (2 <= 5)
    assert [m.video_id for m in selected] == ["bk1", "shared"]
