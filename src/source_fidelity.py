"""Render diagnostic results from retained OCR evidence, not generated narrative."""
import re
from datetime import datetime


class DiagnosticEvidenceError(ValueError):
    """Substantive evidence failure; do not retry it as a formatting correction."""


RESULT_LABEL = re.compile(r'^(?:Impression|Conclusion|Interpretation|Findings|Results)\s*:', re.I)
PAGE_BOUNDARY = re.compile(r'(?m)^=== (?:SOURCE )?PDF PAGE \d+ ===\s*$|\f')
SERVICE_DATE = re.compile(
    r'\b(?:date of (?:service|exam(?:ination)?|study|procedure|collection)|'
    r'(?:service|exam(?:ination)?|study|procedure|collection|report) date|date)\s*:', re.I)
WRONG_DATE_ROLE = re.compile(r'\b(?:birth|DOB|injury|accident|collision)\b', re.I)


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


def _dates(text):
    dates = set()
    for pattern, formats in [
        (r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b', ('%m/%d/%Y', '%m-%d-%Y')),
        (r'\b\d{4}-\d{2}-\d{2}\b', ('%Y-%m-%d',)),
        (r'\b[A-Za-z]+ \d{1,2},? \d{4}\b', ('%B %d %Y', '%b %d %Y')),
    ]:
        for match in re.findall(pattern, ' '.join(text.split())):
            for fmt in formats:
                try:
                    dates.add(datetime.strptime(match.replace(',', ''), fmt).strftime('%m/%d/%Y'))
                    break
                except ValueError:
                    continue
    return dates


def render_diagnostic(entry, documents):
    """Require a same-page date/study/result anchor before rendering a paragraph.

    This narrow check validates quoted OCR support, not OCR correctness, source
    authenticity, or completeness. Ambiguous/multi-page evidence needs review.
    """
    def reject(reason):
        raise DiagnosticEvidenceError('Diagnostic evidence needs review: ' + reason)

    result = entry.get('diagnostic_result')
    fields = {'date', 'facility', 'provider', 'study', 'evidence'}
    if (not isinstance(result, dict) or set(result) != fields
            or set(entry) - {'record_type', 'source_ids', 'diagnostic_result', 'text'}):
        reject('use only the supported header fields and result evidence.')
    for field in fields - {'evidence'}:
        if not isinstance(result[field], str) or not result[field].strip():
            reject('a diagnostic header field is missing.')
    try:
        date = datetime.strptime(result['date'], '%m/%d/%Y').strftime('%m/%d/%Y')
    except ValueError:
        reject('the service date is invalid.')
    evidence = result['evidence']
    if not isinstance(evidence, list) or not evidence:
        reject('a labeled result quote and its dated source are required.')
    ids, quotes = set(), []
    for item in evidence:
        if (not isinstance(item, dict) or set(item) != {'source_id', 'date_quote', 'quote'}
                or any(not isinstance(v, str) or not v.strip() for v in item.values())):
            reject('result evidence is incomplete.')
        sid, date_quote, quote = item['source_id'], item['date_quote'], item['quote']
        if sid not in entry['source_ids'] or sid not in documents:
            reject('the result cites an unknown or unrelated source.')
        if (not SERVICE_DATE.search(date_quote) or _dates(date_quote) != {date}
                or WRONG_DATE_ROLE.search(date_quote)):
            reject('the quoted date does not establish this diagnostic service date.')
        if not RESULT_LABEL.match(quote.strip()) or not quote.split(':', 1)[1].strip():
            reject('quote the labeled result section, not a new indication or plan.')
        content = documents[sid].get('content', '')
        supported = []
        for unit in PAGE_BOUNDARY.split(content):
            normalized = _normalize(unit)
            if not all(_normalize(value) in normalized
                       for value in (date_quote, quote, result['study'])):
                continue
            if _normalize(date_quote) not in {_normalize(line) for line in unit.splitlines()}:
                continue
            if _normalize(quote) not in {_normalize(section) for section in _result_sections(unit)}:
                continue
            # Multiple service dates on one unpartitioned page are ambiguous.
            dated_lines = [line for line in unit.splitlines()
                           if SERVICE_DATE.search(line) and not WRONG_DATE_ROLE.search(line)]
            if any(_dates(line) - {date} for line in dated_lines):
                continue
            if not all(_attribution_supported(field, result[field], unit, content)
                       for field in ('facility', 'provider')):
                continue
            supported.append(unit)
        if not supported:
            reject('the date, study and exact result quote do not align on a source page.')
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
