#!/usr/bin/env python3

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests


SERVICE_HOSTS = {'devnet.cisco.com', 'devnet-testing.cisco.com'}
SERVICE_PATH = '/v1/devenv-community/galileo'
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

    def environment_fields(self):
        return {'GALILEO_LAB_SESSION_TOKEN': self.token,
            'GALILEO_LAB_GATEWAY_URL': self.base_url,
            'GALILEO_LAB_SESSION_EXPIRES_AT': self.expires_at,
            'GALILEO_LAB_SESSION_PROJECT_ID': self.project_id,
            'GALILEO_LAB_SESSION_LOG_STREAM_ID': self.log_stream_id,
            'GALILEO_LAB_SESSION_ENVIRONMENT_ID': self.environment_id}


def validate_session(payload, environ, *, require_current=True):
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
    return WorkshopSession(**{key: payload[key] for key in
        ('token', 'base_url', 'expires_at', 'project_id', 'log_stream_id', 'environment_id')})


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
        ('token', 'GALILEO_LAB_SESSION_TOKEN'), ('base_url', 'GALILEO_LAB_GATEWAY_URL'),
        ('expires_at', 'GALILEO_LAB_SESSION_EXPIRES_AT'), ('project_id', 'GALILEO_LAB_SESSION_PROJECT_ID'),
        ('log_stream_id', 'GALILEO_LAB_SESSION_LOG_STREAM_ID'),
        ('environment_id', 'GALILEO_LAB_SESSION_ENVIRONMENT_ID'))}
    try:
        # An expired descriptor still identifies its original base for renewal.
        return validate_session(payload, env, require_current=False)
    except (ValueError, TypeError):
        raise RuntimeError(SESSION_ERROR) from None


def main():
    try:
        session = bootstrap_session()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name, value in session.environment_fields().items():
        print(name + '=' + value)
    return 0


if __name__ == '__main__':
    sys.exit(main())
