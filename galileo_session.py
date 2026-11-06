#!/usr/bin/env python3

import base64
import hashlib
import os
import secrets
import signal
import threading
import time
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests


SERVICE_HOSTS = {'devnet.cisco.com', 'devnet-testing.cisco.com'}
SERVICE_PATH = '/v1/devenv-community/galileo'
BARRY_BASE = 'https://galileo-gateway.barrysecure.com/v1/galileo'
BARRY_PROXY = BARRY_BASE + '/proxy'
PROJECT_ID = '2e652072-412e-4bef-97a5-1a8f4dc5b68e'
STREAM_ID = '9ac1ae85-1c47-4447-8050-df5fbb5b2ae6'
SESSION_ERROR = 'Workshop session is unavailable. Rerun source 0-init-lab.sh workshop or ask the lab instructor for help.'


def service_origin(url):
    if not isinstance(url, str):
        raise ValueError('invalid service')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc not in SERVICE_HOSTS
            or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('invalid service')
    return 'https://' + parsed.netloc


def bootstrap_url(environ):
    explicit = environ.get('GALILEO_LAB_BOOTSTRAP_URL', '').strip()
    if explicit:
        origin = service_origin(explicit)
        if explicit != origin + SERVICE_PATH + '/sessions':
            raise ValueError('invalid bootstrap path')
        return explicit
    origin = service_origin(environ.get('LLM_BASE_URL', ''))
    return origin + SERVICE_PATH + '/sessions'


def environment_id(environ):
    hostname = environ.get('HOSTNAME', '')
    if not re.fullmatch(r'devpod-[A-Za-z0-9][A-Za-z0-9-]{0,127}', hostname):
        raise ValueError('invalid environment')
    return hostname[len('devpod-'):]


@dataclass(frozen=True)
class WorkshopSession:
    token: str = field(repr=False)
    base_url: str
    expires_at: str
    project_id: str
    log_stream_id: str
    environment_id: str
    renewal_token: str = field(default="", repr=False)

    def environment_fields(self):
        return {'GALILEO_LAB_SESSION_TOKEN': self.token,
            'GALILEO_LAB_SESSION_RENEWAL_TOKEN': self.renewal_token,
            'GALILEO_LAB_GATEWAY_URL': self.base_url,
            'GALILEO_LAB_SESSION_EXPIRES_AT': self.expires_at,
            'GALILEO_LAB_SESSION_PROJECT_ID': self.project_id,
            'GALILEO_LAB_SESSION_LOG_STREAM_ID': self.log_stream_id,
            'GALILEO_LAB_SESSION_ENVIRONMENT_ID': self.environment_id}


def validate_session(payload, environ, *, require_current=True, expected_base=None):
    if expected_base is None:
        expected_base = bootstrap_url(environ).removesuffix('/sessions') + '/proxy'
    if not isinstance(payload, dict):
        raise ValueError('invalid session')
    if (payload.get('base_url') != expected_base or payload.get('project_id') != PROJECT_ID
            or payload.get('log_stream_id') != STREAM_ID
            or payload.get('environment_id') != environment_id(environ)):
        raise ValueError('invalid session destination')
    token = payload.get('token')
    if (not isinstance(token, str) or not token.strip() or len(token) > 16384
            or any(ord(c) < 32 or ord(c) > 126 for c in token)):
        raise ValueError('invalid session token')
    expiry = payload.get('expires_at')
    if not isinstance(expiry, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', expiry):
        raise ValueError('invalid session expiry')
    expires = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
    if expires.tzinfo is None or (require_current and expires <= datetime.now(timezone.utc)):
        raise ValueError('invalid session expiry')
    renewal = payload.get('renewal_token', '')
    if expected_base == BARRY_PROXY:
        if (not isinstance(renewal, str) or not renewal.strip()
                or any(ord(c) < 32 or ord(c) > 126 for c in renewal)
                or expires > datetime.now(timezone.utc) + timedelta(hours=1, seconds=30)):
            raise ValueError('invalid renewal session')
    else:
        renewal = ''
    return WorkshopSession(**{key: payload[key] for key in
        ('token', 'base_url', 'expires_at', 'project_id', 'log_stream_id', 'environment_id')}, renewal_token=renewal)


def bootstrap_session(environ=None):
    env = os.environ if environ is None else environ
    try:
        url = bootstrap_url(env) + '/' + environment_id(env)
        user, password = env.get('DEVENV_USER', ''), env.get('DEVENV_PASSWORD', '')
        if not user or not password:
            raise ValueError('missing environment credentials')
        response = requests.post(url, auth=(user, password), timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('bootstrap failed')
        return validate_session(response.json(), env)
    except (ValueError, TypeError, requests.RequestException):
        # Service and transport errors can contain authentication material.
        raise RuntimeError(SESSION_ERROR) from None


def session_from_env(environ=None):
    env = os.environ if environ is None else environ
    payload = {name: env.get(key, '') for name, key in (
        ('token', 'GALILEO_LAB_SESSION_TOKEN'), ('renewal_token', 'GALILEO_LAB_SESSION_RENEWAL_TOKEN'),
        ('base_url', 'GALILEO_LAB_GATEWAY_URL'),
        ('expires_at', 'GALILEO_LAB_SESSION_EXPIRES_AT'), ('project_id', 'GALILEO_LAB_SESSION_PROJECT_ID'),
        ('log_stream_id', 'GALILEO_LAB_SESSION_LOG_STREAM_ID'),
        ('environment_id', 'GALILEO_LAB_SESSION_ENVIRONMENT_ID'))}
    try:
        # An expired descriptor still identifies its original base for renewal.
        expected = None
        if payload['base_url'] == BARRY_PROXY:
            if env.get('GALILEO_LAB_BOOTSTRAP_URL', '') not in ('', BARRY_BASE + '/connections'):
                raise ValueError('session service changed')
            expected = BARRY_PROXY
        return validate_session(payload, env, require_current=False, expected_base=expected)
    except (ValueError, TypeError):
        raise RuntimeError(SESSION_ERROR) from None


class NoAutomaticAuth(requests.auth.AuthBase):
    def __call__(self, request):
        return request


def _barry_post(path, **kwargs):
    return requests.post(BARRY_BASE + path, auth=NoAutomaticAuth(), allow_redirects=False, **kwargs)


class SessionRenewalRejected(RuntimeError):
    pass


class _ConnectDeadline(Exception):
    pass


def _raise_connect_deadline(signum, frame):
    raise _ConnectDeadline()


def _connect_post(path, deadline, **kwargs):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ConnectDeadline()
    use_alarm = (hasattr(signal, 'setitimer') and threading.current_thread() is threading.main_thread()
        and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
    previous_handler = signal.getsignal(signal.SIGALRM) if use_alarm else None
    try:
        if use_alarm:
            signal.signal(signal.SIGALRM, _raise_connect_deadline)
            signal.setitimer(signal.ITIMER_REAL, remaining)
        result = _barry_post(path, timeout=min(30, remaining), **kwargs)
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    if time.monotonic() >= deadline:
        raise _ConnectDeadline()
    return result


def renew_barrysecure_session(session, environ=None):
    env = os.environ if environ is None else environ
    try:
        if (session.base_url != BARRY_PROXY or not session.renewal_token
                or env.get('GALILEO_LAB_BOOTSTRAP_URL', '') not in ('', BARRY_BASE + '/connections')
                or session.environment_id != environment_id(env)):
            raise ValueError('session service changed')
        result = _barry_post('/sessions/' + session.environment_id + '/renew',
            headers={'Authorization': 'Bearer ' + session.renewal_token}, timeout=30)
        if result.status_code in (401, 403, 404, 410):
            raise SessionRenewalRejected(SESSION_ERROR)
        if result.status_code != 200:
            raise ValueError('renewal failed')
        renewed = validate_session(result.json(), env, expected_base=BARRY_PROXY)
        if renewed.renewal_token != session.renewal_token:
            raise ValueError('renewal identity changed')
        return renewed
    except (ValueError, TypeError, requests.RequestException):
        raise RuntimeError(SESSION_ERROR) from None


def connect_session(environ):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    try:
        deadline = time.monotonic() + 600
        result = _connect_post('/connections', deadline, json={'environment_id': environment_id(environ),
            'challenge': challenge})
        if result.status_code != 201:
            raise ValueError('connection failed')
        value = result.json()
        if not isinstance(value, dict):
            raise ValueError('invalid connection')
        request_id = value.get('connection_id', '')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', request_id):
            raise ValueError('invalid connection')
        expected_link = 'https://galileo-gateway.barrysecure.com/connect?request=' + request_id
        if value.get('verification_url') != expected_link or value.get('poll_interval') != 2:
            raise ValueError('invalid connection link')
        expiry = value.get('expires_at', '')
        if not isinstance(expiry, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', expiry):
            raise ValueError('invalid connection expiry')
        seconds = (datetime.fromisoformat(expiry.replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds()
        if not 0 < seconds <= 630:
            raise ValueError('invalid connection expiry')
        deadline = min(deadline, time.monotonic() + seconds)
        print('Connect this lab once in your browser:\n' + expected_link, file=sys.stderr)
        print('Waiting for Connect to finish...', file=sys.stderr)
        while time.monotonic() < deadline:
            try:
                poll = _connect_post('/connections/' + request_id + '/poll', deadline,
                    json={'verifier': verifier})
            except requests.RequestException:
                poll = None
            if poll is not None and poll.status_code == 200:
                return validate_session(poll.json(), environ, expected_base=BARRY_PROXY)
            if poll is not None and poll.status_code != 503:
                if poll.status_code != 202 or poll.json() != {'status': 'pending'}:
                    raise ValueError('connection declined')
            time.sleep(max(0, min(2, deadline - time.monotonic())))
        raise ValueError('connection expired')
    except (ValueError, TypeError, requests.RequestException, _ConnectDeadline):
        raise RuntimeError(SESSION_ERROR) from None


def initialize_session(environ=None):
    env = os.environ if environ is None else environ
    explicit = env.get('GALILEO_LAB_BOOTSTRAP_URL', '')
    if explicit and explicit != BARRY_BASE + '/connections':
        return bootstrap_session(env)
    if env.get('GALILEO_LAB_SESSION_TOKEN'):
        session = session_from_env(env)
        if session.base_url != BARRY_PROXY:
            return bootstrap_session(env)
        try:
            return renew_barrysecure_session(session, env)
        except SessionRenewalRejected:
            return connect_session(env)
    return connect_session(env)


def main():
    try:
        session = initialize_session()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name, value in session.environment_fields().items():
        print(name + '=' + value)
    return 0


if __name__ == '__main__':
    sys.exit(main())
