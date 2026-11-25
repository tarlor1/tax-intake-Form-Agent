import pytest

from src.sigma_demo import app as practice_sigma


@pytest.fixture(autouse=True)
def isolated_provider_config(monkeypatch, tmp_path):
    monkeypatch.setattr(practice_sigma, 'PROJECT_ROOT', tmp_path)
    monkeypatch.delenv('SIGMA_PRACTICE_PROVIDER', raising=False)
    for provider, (key, model, *_rest) in practice_sigma._LIVE_PROVIDERS.items():
        for name in (key, model, f'SIGMA_PRACTICE_{provider.upper()}_MODEL'):
            monkeypatch.delenv(name, raising=False)


def test_one_existing_gemini_config_is_selected(tmp_path):
    (tmp_path / '.env').write_text('GEMINI_API_KEY=synthetic-key\nGEMINI_MODEL=gemini-test\n')
    policy, key = practice_sigma._live_settings(None)
    assert (policy.identity.provider, policy.identity.model, policy.identity.endpoint) == (
        'gemini', 'gemini-test', 'https://generativelanguage.googleapis.com',
    )
    assert key == 'synthetic-key'


@pytest.mark.parametrize(('provider', 'key_name', 'model_name'), [
    ('openai', 'OPENAI_API_KEY', 'OPENAI_MODEL'),
    ('anthropic', 'ANTHROPIC_API_KEY', 'CLAUDE_MODEL'),
])
def test_explicit_provider_overrides_multiple_keys_without_fallback(monkeypatch, provider, key_name, model_name):
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-gemini')
    monkeypatch.setenv(key_name, 'synthetic-selected')
    monkeypatch.setenv(model_name, 'tool-model')
    with pytest.raises(practice_sigma.PracticeError, match='select --provider'):
        practice_sigma._live_settings(None)
    policy, key = practice_sigma._live_settings(provider)
    assert (policy.identity.provider, policy.identity.model, key) == (provider, 'tool-model', 'synthetic-selected')
    monkeypatch.delenv(key_name)
    with pytest.raises(practice_sigma.PracticeError, match=key_name):
        practice_sigma._live_settings(provider)
