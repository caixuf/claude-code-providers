"""The name the client asks for, and the name the bridge answers to.

A client's model list is a copy of the bridge's, taken when it was configured.
When the curation narrows, the copy keeps asking for an id LiteLLM no longer
has and the answer is ``400 Invalid model name passed in model=...`` - an error
that names the model and tells a configured client to look elsewhere.

The bridge resolves it the way an operator would: if the requested name is not
served and exactly one model of that provider is, that is the one being asked
for. Ambiguity is left alone, because quietly answering a different model is
worse than failing.
"""
from __future__ import annotations

import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import model_remap  # noqa: E402

SERVED = {
    "qianfan-deepseek-v4.1-flash",
    "qianfan-deepseek-v4.1-flash[1m]",
    "cline-deepseek-v4.1-flash",
    "main",
    "pro",
}


def test_the_served_name_is_left_alone():
    assert model_remap.serves("qianfan-deepseek-v4.1-flash", SERVED)
    model, note = model_remap.remap_model("qianfan-deepseek-v4.1-flash", SERVED)
    assert note is None
    assert model == "qianfan-deepseek-v4.1-flash"


def test_the_marker_and_the_case_do_not_decide_served():
    assert model_remap.serves("Qianfan-DeepSeek-V4.1-Flash[1M]", SERVED)


def test_an_old_spelling_resolves_to_the_one_model_that_provider_serves():
    model, note = model_remap.remap_model("qianfan-deepseek-v4-flash[1m]", SERVED)
    assert model == "qianfan-deepseek-v4.1-flash"
    assert note == "qianfan-deepseek-v4-flash[1m] -> qianfan-deepseek-v4.1-flash"


def test_the_retired_1017_name_resolves_too():
    model, _ = model_remap.remap_model("qianfan-deepseek-v4.1-flash-1017[1m]", SERVED)
    assert model == "qianfan-deepseek-v4.1-flash"


def test_a_bare_alias_is_never_remapped():
    """`main` is a routing group, not a provider - it has no prefix to match."""
    assert model_remap.remap_model("main", SERVED) == ("main", None)
    assert model_remap.remap_model("turbo", SERVED) == ("turbo", None)


def test_two_models_of_one_provider_leave_the_name_alone():
    """Guessing between two is worse than failing: the caller asked for one."""
    two = {"cline-deepseek-v4.1-flash", "cline-glm-5.3"}
    assert model_remap.remap_model("cline-deepseek-v4-pro", two) == (
        "cline-deepseek-v4-pro",
        None,
    )


def test_another_providers_name_is_not_borrowed():
    model, note = model_remap.remap_model("minimax-something", SERVED)
    assert (model, note) == ("minimax-something", None)


def test_an_empty_served_set_disables_the_remap():
    assert model_remap.remap_model("qianfan-deepseek-v4-flash", set()) == (
        "qianfan-deepseek-v4-flash",
        None,
    )
