"""Bound source chunks without destroying their physical-page provenance."""
import re


PAGE_MARKER = re.compile(r'(?m)^=== (?:SOURCE )?PDF PAGE (\d+) ===[ \t]*\r?$|\f')

def source_prompt_text(document):
    """Present context separately; only content supplies dates and result evidence."""
    context = ''
    if document.get('reference_context'):
        context = ('[REFERENCE CONTEXT — ATTRIBUTION ONLY. Not date/result evidence; '
                   'do not create entries from this header.]\n' +
                   document['reference_context'] + '\n[END REFERENCE CONTEXT]\n')
    warning = ('[PARTIAL SOURCE PAGE: diagnostic result completeness cannot be established; '
               'flag diagnostic material for manual review.]\n'
               if document.get('incomplete_page') else '')
    return context + warning + document['content']


def page_units(content):
    """Yield exact source slices and recorded page numbers, never invented ones."""
    markers = list(PAGE_MARKER.finditer(content))
    if not markers:
        if content:
            yield None, content
        return
    if markers[0].start():
        yield None, content[:markers[0].start()]
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
        yield int(marker.group(1)) if marker.group(1) else None, content[marker.start():end]


def _fragments(text, limit):
    """Keep lines intact where possible, but also bound an oversized single line."""
    current = ''
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            yield current
            current = ''
        while len(line) > limit:
            yield line[:limit]
            line = line[limit:]
        current += line
    if current:
        yield current


def chunk_source(filename, content, max_chunk_chars):
    """Keep complete pages together; mark every partial page as incomplete evidence.

    Carried header context is a separate attribution-only field, not source body.
    Joining the chunks' content reproduces the original text exactly.
    """
    if max_chunk_chars < 1:
        raise ValueError('Source chunk size must be positive.')
    chunks, current, pages = [], '', []

    def save(text, numbers, incomplete=False):
        chunks.append({'filename': filename, 'content': text,
                       'source_pages': sorted(set(numbers)),
                       'incomplete_page': incomplete})

    for page, unit in page_units(content):
        numbers = [page] if page is not None else []
        if len(unit) > max_chunk_chars:
            if current:
                save(current, pages)
                current, pages = '', []
            for fragment in _fragments(unit, max_chunk_chars):
                save(fragment, numbers, incomplete=True)
        else:
            if current and len(current) + len(unit) > max_chunk_chars:
                save(current, pages)
                current, pages = '', []
            current += unit
            pages.extend(numbers)
    if current or not chunks:
        save(current, pages)
    if len(chunks) > 1:
        for index, chunk in enumerate(chunks, 1):
            chunk['filename'] = f'{filename} (part {index})'
            if index > 1:
                chunk['reference_context'] = content[:1500].strip()
    return chunks
