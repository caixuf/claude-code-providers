"""The Qianfan schedule must not overwrite the selected fast route."""
from pathlib import Path
import importlib.util

import yaml
import sys


def test_flash_remains_beam_across_schedule_states():
    root = Path(__file__).resolve().parents[2]
    default = yaml.safe_load((root / 'registry' / 'profiles.yaml').read_text())
    spec = importlib.util.spec_from_file_location('qianfan_window', root / 'bridge' / 'qianfan_window.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    night = yaml.safe_load(module.ON)
    assert default['groups']['flash'] == {'members': ['beam-free']}
    assert night['groups']['flash'] == {'members': ['beam-free']}
    assert 'qianfan-deepseek-v4.1-flash-1017' in night['groups']['main']['members']
    assert 'qianfan-deepseek-v4.1-flash-1017' in night['groups']['pro']['members']


def test_flash_group_inherits_beam_compatibility_rules():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'bridge'))
    import render_config
    doc = yaml.safe_load((root / 'registry' / 'models.yaml').read_text())
    profiles = yaml.safe_load((root / 'registry' / 'profiles.yaml').read_text())
    models, providers = render_config.index_models(doc)
    emitted = {}
    render_config.render_config(models, providers, profiles, emitted)
    assert 'flash' in render_config.render_media_rules(models, profiles, emitted)
    assert render_config.render_field_rules(models, profiles, emitted).get('flash') == ['cache_control']
    assert render_config.render_clamp(models, providers, profiles)['flash'] == 131072
