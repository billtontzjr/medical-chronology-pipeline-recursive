"""Bounded response-limit recovery with private, stage-bound diagnostics."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

_sink = ContextVar('response_diagnostics', default=None)


class IncompleteResponseError(RuntimeError):
    def __init__(self, provider, reason, budget):
        self.provider, self.reason, self.budget = provider, reason, budget
        super().__init__(f'{provider} returned an incomplete response ({reason}; output budget {budget}). '
                         'No partial response was accepted. Completed work is saved; review the response diagnostics before retrying.')


@contextmanager
def capture_responses(callback):
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


def record_response(response, provider, model, reason, budget, text, retry):
    callback = _sink.get()
    if callback is None:
        return
    usage = getattr(response, 'usage', None)
    # Never retain hidden reasoning, API keys, headers, or full request prompts.
    counts = {key: value for key in ('input_tokens', 'output_tokens', 'total_tokens')
              if isinstance(value := getattr(usage, key, None), int)}
    callback({'provider': provider, 'model': model, 'stop_reason': reason,
              'output_budget': budget, 'usage': counts, 'response': text,
              'retry_with_larger_budget': retry,
              'recorded_at': datetime.now(timezone.utc).isoformat()})
