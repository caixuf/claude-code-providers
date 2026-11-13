"""The needle has to sit at the very end, or the probe proves nothing."""
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import ctx_probe  # noqa: E402


def test_the_code_is_the_last_thing_the_model_sees():
    prompt = ctx_probe.build_prompt(10_000)
    assert prompt.rstrip().endswith("Only the code:")
    assert prompt.index(ctx_probe.CODE) > len(prompt) - 100


def test_the_body_scales_with_the_target():
    small = ctx_probe.build_prompt(10_000)
    large = ctx_probe.build_prompt(100_000)
    assert len(large) > len(small) * 8


def test_estimate_is_in_the_same_ballpark_as_the_target():
    for target in (1_000, 10_000, 100_000):
        approx = ctx_probe.estimate_tokens(ctx_probe.build_prompt(target))
        assert target * 0.9 <= approx <= target * 1.1, (target, approx)


def test_the_output_prompt_demands_the_whole_list():
    prompt = ctx_probe.build_output_prompt(20_000)
    assert "1 to 20000" in prompt
    assert "Do not add commentary" in prompt
    assert "do not stop early" in prompt
