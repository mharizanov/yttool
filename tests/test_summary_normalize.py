from yttool.summarize import normalize_video_summary


def test_normalizes_messy_llm_output():
    data = {
        "title": "A   Title | with pipe",
        "tl_dr": "Short  takeaway.",
        "sections": [
            {"heading": "Main ideas", "bullets": [{"text": "point one", "source_ids": ["S0001", "bogus"]}, "bare string bullet"]},
            {"heading": "", "bullets": [{"text": "orphan"}]},
        ],
        "claims": [{"claim": "X is true", "stance": "WILD_GUESS", "source_ids": "S0002"}],
        "themes": ["beekeeping", ""],
        "watch_verdict": {"score_1_5": "9", "reason": "great"},
        "nodes": [{"label": "root", "source_ids": ["S0001"], "children": ["child string"]}],
    }
    out = normalize_video_summary(data, "fallback")
    assert out["title"] == "A Title - with pipe"
    assert out["sections"][0]["bullets"][0]["source_ids"] == ["S0001"]
    assert out["sections"][0]["bullets"][1]["text"] == "bare string bullet"
    assert len(out["sections"]) == 1  # empty-heading section dropped
    assert out["claims"][0]["stance"] == "asserted"
    assert out["claims"][0]["source_ids"] == ["S0002"]
    assert out["themes"] == ["beekeeping"]
    assert out["watch_verdict"]["score_1_5"] == 5  # clamped
    assert out["nodes"][0]["children"][0]["label"] == "child string"


def test_empty_input():
    out = normalize_video_summary({}, "fallback title")
    assert out["title"] == "fallback title"
    assert out["sections"] == []
    assert out["watch_verdict"]["score_1_5"] == 3
