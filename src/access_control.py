"""Fail-closed shared-team access gate. No patient data is loaded before this gate."""
import hashlib
import hmac
import os
import threading
import time

SESSION_SECONDS = 8 * 60 * 60
_attempts = []
_attempt_lock = threading.Lock()


def configured_password(environ=None):
    value = (os.environ if environ is None else environ).get('TEAM_PASSWORD', '')
    return value if len(value) >= 16 else None


def password_matches(candidate, expected):
    return bool(expected) and hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(expected.encode()).digest())


def session_valid(session, password, now=None):
    now = time.time() if now is None else now
    expected = hashlib.sha256(password.encode()).hexdigest() if password else ''
    return (bool(expected) and session.get('team_password_version') == expected
            and 0 <= now - session.get('team_signed_in_at', 0) < SESSION_SECONDS)


def attempt_allowed(now=None):
    """Process-wide throttle also applies to newly opened browser sessions."""
    now = time.monotonic() if now is None else now
    with _attempt_lock:
        _attempts[:] = [stamp for stamp in _attempts if now - stamp < 60]
        if len(_attempts) >= 10:
            return False
        _attempts.append(now)
        return True


def require_team_access(st):
    gateway = os.getenv('APP_GATEWAY_SECRET')
    if gateway:
        supplied = st.context.headers.get('X-Chronology-Gateway', '')
        if not hmac.compare_digest(supplied, gateway):
            st.error('Open the app through the team sign-in page.')
            st.stop()
        st.sidebar.link_button('Sign out', '/logout')
        return
    if os.getenv('RENDER'):
        st.error('Start the protected server with python serve.py.')
        st.stop()
    password = configured_password()
    if not password:
        st.error('Team access is not configured. Ask the administrator to set a team password of at least 16 characters.')
        st.stop()
    if session_valid(st.session_state, password):
        if st.sidebar.button('Sign out'):
            st.session_state.clear()
            st.rerun()
        return
    # Remove stale run selections and any sensitive session values on expiry.
    if st.session_state.get('team_signed_in_at'):
        st.session_state.clear()
    st.title('Medical Chronology — Team sign-in')
    with st.form('team_sign_in', clear_on_submit=True):
        candidate = st.text_input('Team password', type='password')
        submitted = st.form_submit_button('Sign in')
    if submitted:
        if not attempt_allowed():
            st.error('Too many sign-in attempts. Wait one minute and try again.')
        elif password_matches(candidate, password):
            st.session_state['team_signed_in_at'] = time.time()
            st.session_state['team_password_version'] = hashlib.sha256(password.encode()).hexdigest()
            st.rerun()
        else:
            st.error('The password was not accepted.')
    st.stop()
