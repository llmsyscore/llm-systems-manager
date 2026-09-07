"""#878: pure parsers for generation_config.json, model cards, repo resolution."""
from __future__ import annotations

import model_meta as mm

QWEN_CARD = """---
library_name: transformers
license: apache-2.0
base_model:
- Qwen/Qwen3-30B-A3B-Instruct-2507
pipeline_tag: text-generation
---

# Qwen3-30B-A3B-Instruct-2507

## Quickstart

Some text.

## Best Practices

To achieve optimal performance, we recommend the following settings:

1. **Sampling Parameters**: We suggest using `Temperature=0.7`, `TopP=0.8`, `TopK=20`, and `MinP=0`.
   - For supported frameworks, you can adjust the `presence_penalty` parameter between 0 and 2 to reduce endless repetitions.

## Citation
"""

TABLE_CARD = """---
base_model: meta-llama/Llama-3.3-70B-Instruct
---
## Recommended settings

| Parameter | Value |
|---|---|
| temperature | 0.6 |
| top_p | 0.9 |
| repetition_penalty | 1.05 |
"""

CLI_CARD = """# Model

Run with `llama-server --temp 0.15 --top-k 40 --min-p 0.1`.
"""


def test_generation_config_maps_keys_and_ignores_others():
    text = '{"bos_token_id": 1, "do_sample": true, "temperature": 0.7, "top_k": 20, "top_p": 0.8, "repetition_penalty": 1.05}'
    assert mm.parse_generation_config(text) == {
        "temperature": 0.7, "top-k": 20.0, "top-p": 0.8, "repeat-penalty": 1.05}


def test_generation_config_bad_json_or_non_numbers():
    assert mm.parse_generation_config("not json") == {}
    assert mm.parse_generation_config('{"temperature": "hot", "top_p": null}') == {}


def test_card_prose_with_heading_and_frontmatter_base_model():
    got = mm.parse_card(QWEN_CARD)
    assert got["values"] == {"temperature": 0.7, "top-p": 0.8, "top-k": 20.0, "min-p": 0.0}
    assert got["context"] == "Best Practices"
    assert got["base_model"] == "Qwen/Qwen3-30B-A3B-Instruct-2507"


def test_card_table_and_scalar_frontmatter():
    got = mm.parse_card(TABLE_CARD)
    assert got["values"] == {"temperature": 0.6, "top-p": 0.9, "repeat-penalty": 1.05}
    assert got["context"] == "Recommended settings"
    assert got["base_model"] == "meta-llama/Llama-3.3-70B-Instruct"


def test_card_cli_flags():
    got = mm.parse_card(CLI_CARD)
    assert got["values"] == {"temperature": 0.15, "top-k": 40.0, "min-p": 0.1}
    assert got["context"] == "Model"
    assert got["base_model"] is None


def test_card_first_match_wins_and_prose_without_number_is_ignored():
    text = "## A\ntemperature: 0.5\n## B\ntemperature = 0.9\npresence_penalty between 0 and 2\n"
    got = mm.parse_card(text)
    assert got["values"] == {"temperature": 0.5}
    assert got["context"] == "A"


def test_resolve_repo_prefers_section_then_model_id():
    assert mm.resolve_repo("x", {"hf-repo": "unsloth/Qwen3-GGUF", "hf-file": "q4"}) == "unsloth/Qwen3-GGUF"
    assert mm.resolve_repo("x", {"--hf-repo": "a/b"}) == "a/b"
    assert mm.resolve_repo("unsloth/Qwen3-GGUF:Q4_K_M", {}) == "unsloth/Qwen3-GGUF"
    assert mm.resolve_repo("unsloth/Qwen3-GGUF", None) == "unsloth/Qwen3-GGUF"
    assert mm.resolve_repo("local-model", {}) is None


def test_pick_base_model_accepts_str_or_list_and_skips_self():
    assert mm.pick_base_model(None, ["Qwen/Q", "other/x"]) == "Qwen/Q"
    assert mm.pick_base_model("meta/L", "Qwen/Q") == "meta/L"
    assert mm.pick_base_model(None, "not a repo") is None
    assert mm.pick_base_model(None, []) is None
