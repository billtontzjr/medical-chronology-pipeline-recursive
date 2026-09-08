"""Single writer per session, released automatically after a crash."""
from contextlib import contextmanager
import fcntl


@contextmanager
def session_lock(path):
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('This case is already being processed. Wait for it to finish or pause it before making changes.')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
