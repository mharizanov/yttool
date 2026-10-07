from yttool.prompts_io import OUTPUT_LANG_PROMPT_NAMES, PROMPT_NAMES, load_prompts


def test_directive_appended_to_reader_facing_prompts():
    prompts, _ = load_prompts(output_lang="Bulgarian")
    for name in OUTPUT_LANG_PROMPT_NAMES:
        assert "OUTPUT LANGUAGE OVERRIDE" in prompts[name]
        assert "in Bulgarian," in prompts[name]
    for name in PROMPT_NAMES:
        if name not in OUTPUT_LANG_PROMPT_NAMES:
            assert "OUTPUT LANGUAGE OVERRIDE" not in prompts[name]


def test_hash_is_language_specific():
    _, default_hash = load_prompts()
    _, bg_hash = load_prompts(output_lang="Bulgarian")
    _, en_hash = load_prompts(output_lang="English")
    assert default_hash != bg_hash != en_hash
    assert default_hash != en_hash
    # deterministic per language
    assert load_prompts(output_lang="Bulgarian")[1] == bg_hash


def test_no_lang_is_unchanged():
    prompts, _ = load_prompts()
    assert all("OUTPUT LANGUAGE OVERRIDE" not in text for text in prompts.values())
