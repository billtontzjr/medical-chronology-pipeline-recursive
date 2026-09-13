"""Read bounded encounter-date fields from preserved clinical header layout."""

import re

from .diagnostic_dates import full_dates, resolve_source_date

_ROLE = (
    r"date[ \t]+of[ \t]+(?:service|visit|exam(?:ination)?|procedure)|"
    r"(?:service|visit|exam(?:ination)?|procedure)[ \t]+date|DOS|DOE"
)
_VALUE = (
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})|"
    r"[A-Za-z]+[ \t]+\d{1,2},?[ \t]+\d{4}"
)
_FIELD = re.compile(
    r"(?:^|\n|[ \t]{3,}|\|)[ \t]*"
    r"(?:Encounter[ \t]*-[ \t]*Office[ \t]+Visit[ \t]+)?"
    r"(?P<role>" + _ROLE + r"|DATE)(?:[ \t]*:[ \t\r\n]*|[ \t]+)"
    r"(?P<value>" + _VALUE + r")(?![\w/-])"
    r"[ \t]*(?=$|\r?\n|\||(?:Patient|DOB|PRN|MRN|Sex|Age)[ \t]*[:#]|-[ \t]+[A-Za-z])",
    re.I,
)


def encounter_fields(text):
    return [
        (match['role'].casefold() != 'date', match['value'])
        for match in _FIELD.finditer(text)
    ]


def supports_header_date(quoted_source, date, page, corroborating_dates=()):
    """A broad quote may contain a narrow date field and adjacent demographics.

    The caller must first locate the exact quoted span in the original page.
    Generic DATE cells require an independently labeled encounter date on that
    same page. No birthday, signature, filename or clock supplies a century.
    """
    quoted = encounter_fields(quoted_source)
    if not quoted:
        return False
    fields = encounter_fields(page)
    corroborated = set(corroborating_dates)
    corroborated.update(d for _, value in fields for d in full_dates(value))
    resolve = lambda value: resolve_source_date(value, corroborated)
    if any(resolve(value) != date for _, value in quoted):
        return False
    if any(explicit for explicit, _ in quoted):
        return True
    explicit_values = [value for explicit, value in fields if explicit]
    return bool(explicit_values) and all(resolve(value) == date for value in explicit_values)
