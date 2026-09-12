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


def claude_response(reason, text='partial', tokens=6000):
    return SimpleNamespace(stop_reason=reason,
        content=[SimpleNamespace(type='thinking', thinking='private reasoning'),
                 SimpleNamespace(type='text', text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=tokens))


def test_opus_headroom_and_one_retry_never_returns_partial_text():
    from src.response_recovery import capture_responses
    a = agent('anthropic', None)
    a.model = 'claude-opus-5'
    a.client.messages.create.side_effect = [claude_response('max_tokens', 'bad partial'),
                                          claude_response('end_turn', 'Complete JSON')]
    records = []
    with capture_responses(records.append):
        assert a._call_api_with_retry('source', max_tokens=6000) == 'Complete JSON'
    calls = a.client.messages.create.call_args_list
    assert [c.kwargs['max_tokens'] for c in calls] == [22000, 44000]
    assert calls[0].kwargs['messages'] == calls[1].kwargs['messages']
    assert all(c.kwargs['model'] == 'claude-opus-5' for c in calls)
    assert records[0]['stop_reason'] == 'max_tokens'
    assert records[0]['response'] == 'bad partial'
    assert 'private reasoning' not in str(records)
    assert 'source' not in str(records)


@pytest.mark.parametrize('reason', ['refusal', 'pause_turn', 'stop_sequence', None, 'end_turn'])
def test_non_token_stops_do_not_retry(reason):
    a = agent('anthropic', claude_response(reason, ''))
    with pytest.raises(RuntimeError, match='incomplete'):
        a._call_api_with_retry('source', max_tokens=6000)
    assert a.client.messages.create.call_count == 1


def test_token_recovery_is_bounded_and_cap_is_respected():
    a = agent('anthropic', claude_response('max_tokens'))
    with pytest.raises(RuntimeError, match='max_tokens'):
        a._call_api_with_retry('source', max_tokens=6000)
    assert a.client.messages.create.call_count == 2
    a.client.messages.create.reset_mock()
    with pytest.raises(RuntimeError):
        a._call_api_with_retry('source', max_tokens=128000)
    assert a.client.messages.create.call_count == 1
    assert a.client.messages.create.call_args.kwargs['max_tokens'] == 128000


def test_unknown_legacy_model_limit_is_not_guessed():
    a = agent('anthropic', claude_response('max_tokens'))
    a.model = 'claude-3-example'
    with pytest.raises(RuntimeError):
        a._call_api_with_retry('source', max_tokens=2000)
    assert a.client.messages.create.call_count == 1
    assert a.client.messages.create.call_args.kwargs['max_tokens'] == 2000


def test_openai_only_retries_explicit_output_limit():
    a = agent('openai', None)
    a.client.responses.create.side_effect = [
        SimpleNamespace(status='incomplete', output_text='cut',
                        incomplete_details=SimpleNamespace(reason='max_output_tokens')),
        SimpleNamespace(status='completed', output_text='complete')]
    assert a._call_api_with_retry('source', max_tokens=6000) == 'complete'
    assert [c.kwargs['max_output_tokens'] for c in a.client.responses.create.call_args_list] == [22000, 44000]
    a.client.responses.create.reset_mock(side_effect=True)
    a.client.responses.create.return_value = SimpleNamespace(status='incomplete', output_text='cut',
        incomplete_details=SimpleNamespace(reason='content_filter'))
    with pytest.raises(RuntimeError, match='content_filter'):
        a._call_api_with_retry('source')
    assert a.client.responses.create.call_count == 1
