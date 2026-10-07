from yttool.mindmap import collect_tooltips, normalize_mindmap, write_html


def test_normalize_preserves_tldr():
    data = {
        "title": "T",
        "nodes": [
            {"label": "Root", "tldr": "Broad branch gist.", "children": [
                {"label": "Leaf", "tldr": "Specific point.", "children": []},
                {"label": "No tip", "children": []},
            ]}
        ],
    }
    out = normalize_mindmap(data)
    assert out["nodes"][0]["tldr"] == "Broad branch gist."
    assert out["nodes"][0]["children"][0]["tldr"] == "Specific point."
    assert "tldr" not in out["nodes"][0]["children"][1]


def test_collect_tooltips():
    mindmap = normalize_mindmap(
        {"title": "T", "nodes": [{"label": "Root", "tldr": "Gist.", "children": [{"label": "Leaf", "tldr": "Detail.", "children": []}]}]}
    )
    tips = collect_tooltips(mindmap)
    assert tips == {"Root": "Gist.", "Leaf": "Detail."}


def test_write_html_embeds_tooltips(tmp_path):
    path = tmp_path / "m.html"
    write_html("# T\n- Root", "T", path, tooltips={"Root": "Gist with </script> escape."})
    content = path.read_text()
    assert '"Root"' in content
    assert "<\\/script> escape" in content
    assert "node-tip" in content
    assert "Hover a node" in content


def test_write_html_without_tooltips(tmp_path):
    path = tmp_path / "m.html"
    write_html("# T\n- Root", "T", path)
    assert "var TIPS = {}" in path.read_text()
