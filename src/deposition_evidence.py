"""Exact source-line citations and private, resumable deposition stages."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from .response_recovery import capture_responses, IncompleteResponseError


class EvidenceError(ValueError):
    pass


class TranscriptIndex:
    """IDs identify extracted-text lines, not printed transcript line numbers."""
    def __init__(self, text):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.offsets = []
        offset = 0
        for line in self.lines:
            self.offsets.append(offset)
            offset += len(line)

    def numbered(self, start=0, end=None):
        end = len(self.text) if end is None else end
        return '\n'.join(f'L{i+1:06d}: {line.rstrip()}' for i, line in enumerate(self.lines)
                         if self.offsets[i] < end and self.offsets[i]+len(line) > start)

    def resolve(self, refs, allowed=None):
        if not isinstance(refs, list) or not refs:
            raise EvidenceError('Missing source-line references')
        quotes, locations = [], []
        for ref in refs:
            if (not isinstance(ref, list) or len(ref) != 2
                    or any(type(n) is not int for n in ref)):
                raise EvidenceError('References must be integer [first_line, last_line] pairs')
            first, last = ref
            if not 1 <= first <= last <= len(self.lines) or last-first > 100:
                raise EvidenceError('Source-line reference is outside the transcript or too broad')
            start = self.offsets[first-1]
            end = self.offsets[last-1]+len(self.lines[last-1])
            if allowed is not None and (end <= allowed[0] or start >= allowed[1]
                    or any(self.offsets[i] >= allowed[1] or self.offsets[i]+len(self.lines[i]) <= allowed[0]
                           for i in range(first-1, last))):
                raise EvidenceError('Reference includes lines outside the supplied source section')
            quote = self.text[start:end].strip()
            if len(' '.join(quote.split())) < 4:
                raise EvidenceError('Source-line reference contains no useful text')
            quotes.append(quote)
            locations.append({'first_line': first, 'last_line': last,
                              'start_char': start, 'end_char': end})
        return quotes, locations


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class StageRunner:
    def __init__(self, document, model, call_api, checkpoint=None, progress=None):
        self.call_api = call_api
        self.checkpoint = Path(checkpoint) if checkpoint else None
        self.progress = progress or (lambda _: None)
        self.signature = hashlib.sha256(json.dumps({'protocol': 2, 'model': model,
            'source': document['content'], 'filename': document['filename']}, sort_keys=True).encode()).hexdigest()
        self.state = {'signature': self.signature, 'source_file': document['filename'],
                      'stages': {}, 'rejections': []}
        if self.checkpoint and self.checkpoint.exists():
            saved = json.loads(self.checkpoint.read_text())
            if saved.get('signature') == self.signature:
                self.state = saved

    def save(self):
        if self.checkpoint:
            atomic_json(self.checkpoint, self.state)

    def ask(self, stage, prompt, validator, max_tokens):
        if stage in self.state['stages']:
            self.progress(f'↳ Deposition: reusing checked {stage}')
            return validator(self.state['stages'][stage])
        feedback = ''
        for attempt in (1, 2):
            self.progress(f'↳ Deposition: {stage}, attempt {attempt}/2')
            def retain_response(details):
                self.state.setdefault('response_diagnostics', []).append({'stage': stage, **details})
                self.save()
                if details['retry_with_larger_budget']:
                    self.progress(f'↳ Deposition: {stage} reached the response limit; retrying once with more output space')
            try:
                with capture_responses(retain_response):
                    raw = self.call_api(prompt+feedback, max_tokens=max_tokens)
            except IncompleteResponseError as exc:
                self.state['response_error'] = {'stage': stage, 'error': str(exc)}
                self.save()
                raise
            self.state.pop('response_error', None)
            try:
                data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()))
                result = validator(data)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                # Private session diagnostics, never general application logs.
                self.state['rejections'].append({'stage': stage, 'attempt': attempt,
                    'error': str(exc), 'response': raw})
                self.state['blocked_stage'] = stage
                self.save()
                feedback = ('\n\nYour previous response failed validation: '+str(exc)+
                    '. Return a corrected complete response using only the supplied evidence. '
                    'Do not invent references, remove material testimony to pass validation, or guess. '
                    'Source-line IDs are integers shown after L in the source. If evidence is insufficient, '
                    'return {"review_required":"Explain the unresolved issue"}.')
                if attempt == 2:
                    raise EvidenceError(f'Deposition {stage} needs review after two attempts. '
                        'Completed stages are saved; private diagnostics identify the rejected response.') from exc
            else:
                self.state['stages'][stage] = data
                self.state.pop('blocked_stage', None)
                self.save()
                return result
        raise AssertionError('unreachable')
