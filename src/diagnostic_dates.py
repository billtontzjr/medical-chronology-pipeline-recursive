"""Resolve diagnostic dates from complete source fields, never a century pivot."""
import re
from datetime import datetime


_SERVICE = r'(?:service|exam(?:ination)?|study|procedure|collection|acquisition|visit|referral|IME|independent medical examination)'
_TIME = r'(?:[ \t]*/[ \t]*time)?'
SERVICE_DATE = re.compile(
    rf'^[ \t]*(?:date{_TIME}[ \t]+of[ \t]+{_SERVICE}|'
    rf'{_SERVICE}[ \t]+date{_TIME}|(?:collected|performed|acquired)(?:[ \t]+on)?|'
    rf'DOS|DOE|date{_TIME})(?:[ \t]*:[ \t]*|[ \t]+(?=\S)|[ \t]*$)', re.I | re.M)
WRONG_DATE_ROLE = re.compile(
    r'\b(?:birth|DOB|injury|accident|collision|signed|signature|dictated|'
    r'printed|ordered|reported|report|result)\b', re.I)
_DATE_VALUE = re.compile(
    r'(?P<date>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})|'
    r'[A-Za-z]+ \d{1,2},? \d{4})'
    r'(?:[ T]+(?:\d{1,2}:\d{2}(?::\d{2})?|\d{4})'
    r'(?: ?[AP]M)?(?: ?(?:Z|UTC|GMT|[ECMP][DS]T|[+-]\d{2}:?\d{2}))?)?', re.I)
_SHORT_DATE = re.compile(r'(\d{1,2})([/-])(\d{1,2})\2(\d{2})')


def full_dates(text):
    """Read only explicit four-digit years; no date is inferred from current time."""
    dates = set()
    for pattern, formats in [
        (r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b', ('%m/%d/%Y', '%m-%d-%Y')),
        (r'\b\d{4}-\d{1,2}-\d{1,2}(?=\b|[Tt]\d)', ('%Y-%m-%d',)),
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


def date_value(field):
    """Return the date token in an explicit field, excluding other date roles."""
    field = ' '.join(field.split())
    label = SERVICE_DATE.match(field)
    if not label or WRONG_DATE_ROLE.search(field):
        return None
    remainder = field[label.end():].strip()
    # Procedure logs often carry a start/end time on the same service day.
    # Both complete timestamps must name the same date; a cross-day range
    # remains unresolved instead of silently choosing its first day.
    if re.match(r"^performed\b", field, re.I):
        endpoints = re.split(r"\s+-\s+", remainder)
        if len(endpoints) == 2:
            values = [_DATE_VALUE.fullmatch(endpoint) for endpoint in endpoints]
            if all(values) and values[0].group('date') == values[1].group('date'):
                return values[0].group('date')
            return None
    remainder = re.split(r"\s+-\s+(?=[A-Za-z])", remainder, maxsplit=1)[0]
    value = _DATE_VALUE.fullmatch(remainder)
    return value.group('date') if value else None


def date_fields(unit):
    """Recognize a complete date field on one line or with its value on the next."""
    lines = re.split(r"\n|[ \t]{3,}", unit)
    fields = []
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:performed|collected|acquired)\s+by\b", line, re.I):
            continue  # Clinician attribution, not a service-date field.
        if not SERVICE_DATE.match(line) or WRONG_DATE_ROLE.search(line):
            continue
        field = line.strip()
        label = SERVICE_DATE.match(field)
        if not field[label.end():].strip() and index + 1 < len(lines):
            if _DATE_VALUE.fullmatch(lines[index + 1].strip()):
                field += '\n' + lines[index + 1].strip()
        fields.append(field)
    return fields


def _resolve(value, corroborated):
    if value is None:
        return None
    full = full_dates(value)
    if len(full) == 1:
        return next(iter(full))
    short = _SHORT_DATE.fullmatch(value)
    if not short:
        return None
    month, day, year = int(short[1]), int(short[3]), int(short[4])
    matches = {date for date in corroborated
               if (int(date[:2]), int(date[3:5]), int(date[-2:])) == (month, day, year)}
    return next(iter(matches)) if len(matches) == 1 else None


def supports_service_date(quote, date, unit, corroborating_dates=()):
    """Require the exact field and one unambiguous service date on this page.

    A short year needs an independent, explicit four-digit service date on the
    same page. DOB, report dates, other pages, and the proposed model date cannot
    supply that century. Conflicting or unreadable service fields require review.
    """
    fields = date_fields(unit)
    normalize = lambda text: ' '.join(text.split()).casefold()
    if normalize(quote) not in {normalize(field) for field in fields}:
        return False
    values = [date_value(field) for field in fields]
    corroborated = {date for value in values if value for date in full_dates(value)}
    return bool(values) and all((resolve_source_date(value, corroborating_dates) if corroborating_dates else _resolve(value, corroborated)) == date for value in values)


def resolve_source_date(value, corroborating_dates):
    """Expand a short year only from a single corroborated clinical century.

    Corroboration is passed by the medical workspace from previously source-checked
    encounters, never from filenames, case DOB, the clock, or a model proposal.
    Requiring a nearby year avoids using recent care to invent a remote historical
    century. Exact dates and genuine century conflicts retain their meaning.
    """
    if value is None:
        return None
    full = full_dates(value)
    if len(full) == 1:
        return next(iter(full))
    short = _SHORT_DATE.fullmatch(value)
    if not short:
        return None
    month, day, year = int(short[1]), int(short[3]), int(short[4])
    possible = set()
    for other in corroborating_dates:
        for parsed in full_dates(other):
            known = int(parsed[-4:])
            candidate = known // 100 * 100 + year
            if abs(candidate - known) <= 10:
                try:
                    possible.add(datetime(candidate, month, day).strftime('%m/%d/%Y'))
                except ValueError:
                    pass
    return next(iter(possible)) if len(possible) == 1 else None
