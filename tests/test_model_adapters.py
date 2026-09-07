from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.chronology_agent import ChronologyAgent


def agent(provider, response):
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.provider, a.model = provider, 'gpt-6-astra' if provider == 'openai' else 'claude-fable-5-1'
    a.client, a.logger = Mock(), Mock()
    a.client.responses.create.return_value = response
    a.client.messages.create.return_value = response
    return a


def test_astra_uses_responses_and_rejects_truncation():
    a = agent('openai', SimpleNamespace(status='completed', output_text='Chronology'))
    assert a._call_api_with_retry('records') == 'Chronology'
    kwargs = a.client.responses.create.call_args.kwargs
    assert kwargs['store'] is False
    assert kwargs['reasoning'] == {'effort': 'high'}
    a.client.responses.create.return_value.status = 'incomplete'
    with pytest.raises(RuntimeError, match='incomplete'):
        a._call_api_with_retry('records')


def test_fable_excludes_thinking_and_sampling_and_rejects_truncation():
    a = agent('anthropic', SimpleNamespace(stop_reason='end_turn', content=[
        SimpleNamespace(type='thinking'), SimpleNamespace(type='text', text='Chronology')]))
    assert a._call_api_with_retry('records') == 'Chronology'
    assert 'temperature' not in a.client.messages.create.call_args.kwargs
    a.client.messages.create.return_value.stop_reason = 'max_tokens'
    with pytest.raises(RuntimeError, match='incomplete'):
        a._call_api_with_retry('records')
