"""Keep writes inside the dedicated outputs tree, away from source records."""
import re
from urllib.parse import unquote, urlparse

OUTPUT_ROOT = '/Medical chronology pipeline outputs'


def validate_destination(value, source=''):
    value = (value or '').strip()
    if value.startswith('https://'):
        url = urlparse(value)
        if url.hostname not in ('dropbox.com', 'www.dropbox.com') or not url.path.startswith('/home/'):
            raise ValueError('Use a Dropbox output folder path, not a shared link.')
        value = unquote(url.path[len('/home'):])
    if (not value.startswith('/') or '\\' in value or
            any(p in ('.', '..') for p in value.split('/')) or
            any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid Dropbox output folder path.')
    value = re.sub('/+', '/', value).rstrip('/')
    if not value.casefold().startswith(OUTPUT_ROOT.casefold() + '/'):
        raise ValueError(f'Outputs must be in a case folder beneath {OUTPUT_ROOT}.')
    if source:
        parsed = urlparse(source)
        source_path = source if source.startswith('/') else (
            unquote(parsed.path[len('/home'):]) if parsed.hostname in ('dropbox.com', 'www.dropbox.com')
            and parsed.path.startswith('/home/') else '')
        source_path = re.sub('/+', '/', source_path).rstrip('/').casefold()
        if source_path and (value.casefold() == source_path or value.casefold().startswith(source_path + '/')):
            raise ValueError('The output destination overlaps the source folder.')
    return value
