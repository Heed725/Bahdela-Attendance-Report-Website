"""Server-side login with password hashes, idle expiry and attempt cooldown."""
import hashlib
import hmac
import time

# Passwords are deliberately absent from source. Deployments may override these
# hashes in [admin_accounts] in Streamlit Secrets.
DEFAULT_ACCOUNTS = {'Kanza': 'pbkdf2_sha256$600000$68ed8a50ba23a3e74fd2a4018fc09b08$b6f0e366a284b1a5aaa67b443012eb847f1e6f65f29c20b1aad28eeee587eb43', 'HR': 'pbkdf2_sha256$600000$8ec74257696ba19e189b5d32579394e6$aceca537d3de39c681751bc9eeb285d324e9d5a50ecfa3c8769e5cbac8f75a3d', 'admin': 'pbkdf2_sha256$600000$754f3137a3bc6b414bb61933a08b6e35$d5e06520c96e94bea3cb9b3a77eab5361a379aeac18ae159bcde9fec5113ef0e'}
PERMISSIONS = frozenset({'add', 'delete', 'transfer', 'enroll', 'list'})
IDLE_SECONDS = 1800


def password_hash(password, salt):
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000)
    return f'pbkdf2_sha256$600000${salt}${digest.hex()}'


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100000 <= int(rounds) <= 2000000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False


def active_account(state, now=None):
    now = time.time() if now is None else now
    account = state.get('management_account')
    if not account:
        return None
    if account and now - state.get('management_last_seen', 0) <= IDLE_SECONDS:
        state['management_last_seen'] = now
        return account
    logout(state)
    return None


def logout(state):
    for key in list(state):
        if key.startswith(('management_', 'manage_', 'login_')):
            state.pop(key, None)


def login(state, username, password, overrides=None, now=None):
    now = time.time() if now is None else now
    if now < state.get('auth_retry_at', 0):
        return False, 'Please wait one minute before trying again.'
    accounts = dict(DEFAULT_ACCOUNTS)
    accounts.update(overrides or {})
    encoded = accounts.get(username.strip())
    # Do the same expensive comparison for an unknown account.
    valid = verify_password(password, encoded or next(iter(DEFAULT_ACCOUNTS.values())))
    if not encoded or not valid:
        attempts = state.get('auth_attempts', 0) + 1
        state['auth_attempts'] = attempts
        if attempts >= 5:
            state['auth_retry_at'] = now + 60
            state['auth_attempts'] = 0
        return False, 'Incorrect username or password.'
    state['management_account'] = username.strip()
    state['management_last_seen'] = now
    state['auth_attempts'] = 0
    state.pop('auth_retry_at', None)
    return True, ''


def require_permission(state, action):
    account = active_account(state)
    if not account or action not in PERMISSIONS:
        raise PermissionError('Log in again before managing employees.')
    return account
