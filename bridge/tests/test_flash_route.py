"""The Qianfan schedule must not overwrite the selected fast route."""
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _module(name: str):
    """Import a repo module by path — the bridge is not an installed package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / 'bridge' / f'{name}.py')
    assert spec is not None and spec.loader is not None, name
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _night() -> dict:
    return yaml.safe_load(_module('qianfan_window').ON)


def _committed(name: str) -> dict:
    return yaml.safe_load((ROOT / 'registry' / name).read_text())


def test_schedule_names_only_the_group_it_owns():
    """`main` is the only group a wall-clock timer may rewrite.

    Naming a group in the overlay replaces it outright for the duration of the
    window, so declaring any other group there lets the timer silently revert
    whatever registry/profiles.yaml says. `pro` is a human's pick and `flash` is
    fixed; neither belongs in the schedule.
    """
    assert set(_night()['groups']) == {'main'}, _night()['groups']
    assert 'qianfan-deepseek-v4.1-flash-1017' in _night()['groups']['main']['members']


def test_unnamed_groups_survive_the_window_unchanged():
    """End-to-end: merging the night overlay leaves pro and flash alone."""
    committed = _committed('profiles.yaml')
    merged = _module('render_config').apply_local_overlay(committed, _night())
    assert merged['groups']['main'] == _night()['groups']['main']
    assert merged['groups']['pro'] == committed['groups']['pro']
    assert merged['groups']['flash'] == {'members': ['beam-free']}
    assert set(merged['groups']) == set(committed['groups'])


def test_flash_group_inherits_beam_compatibility_rules():
    render_config = _module('render_config')
    models, providers = render_config.index_models(_committed('models.yaml'))
    profiles = _committed('profiles.yaml')
    emitted = {}
    render_config.render_config(models, providers, profiles, emitted)
    assert 'flash' in render_config.render_media_rules(models, profiles, emitted)
    assert render_config.render_field_rules(models, profiles, emitted).get('flash') == ['cache_control']
    assert render_config.render_clamp(models, providers, profiles)['flash'] == 131072


def test_overlay_keeps_groups_it_does_not_name():
    """A wall-clock overlay must not delete a group it never mentions.

    The overlay used to replace the whole `groups` mapping. During the 21:00
    window that erased every group the overlay did not list -- `pro` vanished
    from the rendered config even though registry/profiles.yaml still declared
    it, so a model a human had pointed the slot at silently stopped existing.
    """
    committed = {'groups': {'main': {'members': ['day']},
                            'pro': {'members': ['chosen']},
                            'flash': {'members': ['beam-free']}}}
    overlay = {'groups': {'main': {'members': ['night']}}}
    groups = _module('render_config').apply_local_overlay(committed, overlay)['groups']
    assert groups['main'] == {'members': ['night']}, 'overlay wins where it speaks'
    assert groups['pro'] == {'members': ['chosen']}, 'unnamed group survives'
    assert set(groups) == {'main', 'pro', 'flash'}


def test_overlay_still_replaces_a_named_groups_members_outright():
    """Naming a group replaces its member list — removal by shorter list works."""
    committed = {'groups': {'main': {'members': ['a', 'b', 'c']}}}
    overlay = {'groups': {'main': {'members': ['a']}}}
    groups = _module('render_config').apply_local_overlay(committed, overlay)['groups']
    assert groups['main']['members'] == ['a']


def test_a_stale_overlay_is_rewritten_not_trusted(tmp_path, monkeypatch):
    """A file left by an older version must not short-circuit the switch.

    `current_state()` only looked for a `- qianfan-...` member, so a file still
    containing `pro` answered "already on" and was left in place -- the stale
    overlay kept overwriting `pro` until the window closed. Staleness is now a
    content comparison, so an upgrade heals itself on the next run.
    """
    window = _module('qianfan_window')
    stale = tmp_path / 'profiles.local.yaml'
    monkeypatch.setattr(window, 'OVERLAY', stale)

    stale.write_text('groups:\n  main:\n    members:\n    - qianfan-x\n'
                     '  pro:\n    members:\n    - qianfan-x\n', encoding='utf-8')
    assert window.current_state() == 'on', 'the old heuristic cannot tell them apart'
    assert window.overlay_is_stale(window.ON), 'but the content differs from ON'

    stale.write_text(window.ON, encoding='utf-8')
    assert not window.overlay_is_stale(window.ON), 'a matching file needs no work'

    stale.unlink()
    assert window.overlay_is_stale(window.ON), 'a missing file must be written'
    assert not window.overlay_is_stale(None), 'off needs no file at all'