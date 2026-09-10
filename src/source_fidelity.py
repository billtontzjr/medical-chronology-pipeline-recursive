"""Render diagnostic results from retained OCR evidence, not generated narrative."""
import re
from datetime import datetime
from .diagnostic_dates import (SERVICE_DATE, WRONG_DATE_ROLE, full_dates as _dates,
                               date_value, supports_service_date)
from .source_pages import page_units


class DiagnosticEvidenceError(ValueError):
    """Substantive evidence failure; do not retry it as a formatting correction."""
    def __init__(self, message, *, code='source_alignment', checks=()):
        super().__init__(message)
        self.code = code
        self.checks = list(checks) or [code]


class DiagnosticStructureError(DiagnosticEvidenceError):
    """Invalid evidence structure or source IDs cannot be deferred as uncertainty."""


RESULT_LABEL = re.compile(r'^(?:Impression|Conclusion|Interpretation|Findings|Results)\s*:', re.I)
PAGE_BOUNDARY = re.compile(r'(?m)^=== (?:SOURCE )?PDF PAGE \d+ ===\s*$|\f')


def _result_sections(unit):
    """Retain whole labeled result sections, not an arbitrary prefix."""
    sections, current = [], []
    for line in unit.splitlines():
        line = line.strip()
        heading = re.match(r'^[A-Za-z][A-Za-z /-]{1,50}:', line)
        if heading and current:
            sections.append(' '.join(current))
            current = []
        if RESULT_LABEL.match(line) or current:
            current.append(line)
    if current:
        sections.append(' '.join(current))
    preferred = [s for s in sections if re.match(r'^(?:Impression|Conclusion|Interpretation)\s*:', s, re.I)]
    return preferred or sections


def _attribution_supported(field, value, unit, content):
    label = re.compile(rf'(?im)^\s*{field}\s*:\s*(.+)$')
    local_values = label.findall(unit)
    all_values = {_normalize(v) for v in label.findall(content)}
    if value == f'{field.capitalize()} not documented':
        return not all_values
    if local_values:
        return _normalize(value) in {_normalize(v) for v in local_values}
    if all_values:
        return all_values == {_normalize(value)}
    return _normalize(value) in _normalize(unit)


def _normalize(text):
    # Preserve punctuation and words; only OCR whitespace/case can differ.
    return ' '.join(text.split()).casefold()


def validate_diagnostic_structure(entry, documents):
    """Check every reference before any uncertain source can be withheld."""
    def reject(reason):
        raise DiagnosticStructureError('Diagnostic evidence needs review: ' + reason,
                                       code='invalid_diagnostic_structure')

    ids = entry.get('source_ids')
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(sid, str) or sid not in documents for sid in ids)
            or len(ids) != len(set(ids))):
        reject('the source references are missing, duplicated or unknown.')
    result = entry.get('diagnostic_result')
    fields = {'date', 'facility', 'provider', 'study', 'evidence'}
    if (not isinstance(result, dict) or set(result) != fields
            or set(entry) - {'record_type', 'source_ids', 'diagnostic_result', 'text'}):
        reject('use only the supported header fields and result evidence.')
    for field in fields - {'evidence'}:
        if not isinstance(result[field], str) or not result[field].strip():
            reject('a diagnostic header field is missing.')
    if 'text' in entry and not isinstance(entry['text'], str):
        reject('diagnostic text must be a string when supplied.')
    evidence = result['evidence']
    if not isinstance(evidence, list) or not evidence:
        reject('a labeled result quote and its dated source are required.')
    for item in evidence:
        if (not isinstance(item, dict) or set(item) != {'source_id', 'date_quote', 'quote'}
                or any(not isinstance(v, str) or not v.strip() for v in item.values())):
            reject('result evidence is incomplete.')
        if item['source_id'] not in ids:
            reject('the result cites an unknown or unrelated source.')
    if {item['source_id'] for item in evidence} != set(ids):
        reject('each cited source must have result evidence.')


def render_diagnostic(entry, documents):
    """Require a complete same-page date/study/result anchor before publication."""
    validate_diagnostic_structure(entry, documents)

    def reject(reason, code='source_alignment', checks=()):
        raise DiagnosticEvidenceError('Diagnostic evidence needs review: ' + reason,
                                      code=code, checks=checks)

    result = entry['diagnostic_result']
    try:
        date = datetime.strptime(result['date'], '%m/%d/%Y').strftime('%m/%d/%Y')
    except ValueError:
        reject('the service date is invalid.', 'invalid_service_date')
    ids, quotes = set(), []
    for item in result['evidence']:
        sid, date_quote, quote = item['source_id'], item['date_quote'], item['quote']
        if date_value(date_quote) is None:
            reject('the quoted date does not establish this diagnostic service date.',
                   'unsupported_date_field')
        if not RESULT_LABEL.match(quote.strip()) or not quote.split(':', 1)[1].strip():
            reject('quote the labeled result section, not a new indication or plan.',
                   'missing_result_label')
        document = documents[sid]
        if document.get('incomplete_page'):
            reject('the source chunk contains only part of a page; review the complete original page.',
                   'incomplete_source_page')
        content = document.get('content', '')
        attribution = content + '\n' + document.get('reference_context', '')
        supported, failures = [], set()
        for _, unit in page_units(content):
            normalized = _normalize(unit)
            checks = {
                'service_date_not_established': supports_service_date(date_quote, date, unit),
                'study_not_on_result_page': _normalize(result['study']) in normalized,
                'result_not_complete_on_page': _normalize(quote) in {
                    _normalize(section) for section in _result_sections(unit)},
                'facility_not_supported': _attribution_supported('facility', result['facility'], unit, attribution),
                'provider_not_supported': _attribution_supported('provider', result['provider'], unit, attribution),
            }
            if not all(checks.values()):
                failures.update(check for check, valid in checks.items() if not valid)
                continue
            supported.append(unit)
        if not supported:
            reject('the date, study and exact result quote do not align on a source page.',
                   checks=sorted(failures))
        ids.add(sid)
        normalized_quote = ' '.join(quote.split())
        if normalized_quote not in quotes:
            quotes.append(normalized_quote)
    if ids != set(entry['source_ids']):
        reject('each cited source must support a result quote.')
    header = [date] + [' '.join(result[field].split()).rstrip('.')
                       for field in ('facility', 'provider', 'study')]
    text = '. '.join(header) + '. ' + ' '.join(quotes)
    if 'text' in entry and (not isinstance(entry['text'], str)
                            or _normalize(entry['text']) != _normalize(text)):
        reject('free-form diagnostic narrative differs from the source-backed rendering.')
    return text
