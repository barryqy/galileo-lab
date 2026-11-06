#!/usr/bin/env python3

import json
import os
import warnings
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import requests

from galileo_session import (BARRY_PROXY, NoAutomaticAuth, SERVICE_HOSTS, SERVICE_PATH, SESSION_ERROR,
    bootstrap_session, bootstrap_url, renew_barrysecure_session, session_from_env)


def api_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [api_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    metadata = value.get("user_metadata")
    if isinstance(metadata, dict):
        # Only telemetry metadata is string-valued; application data stays intact.
        result["user_metadata"] = {name: val if isinstance(val, str) else json.dumps(val)
            for name, val in metadata.items() if val is not None}
    for key in ("traces", "spans"):
        if isinstance(value.get(key), list):
            result[key] = [api_payload(item) for item in value[key]]
    return result


def _body_positions(files, data):
    streams = []
    if files:
        if isinstance(files, dict):
            entries = files.items()
        elif isinstance(files, (list, tuple)):
            entries = files
        else:
            return None
        for _, value in entries:
            if isinstance(value, (list, tuple)):
                value = value[1]
            if hasattr(value, 'read'):
                streams.append(value)
    if hasattr(data, 'read'):
        streams.append(data)
    elif data is not None and not isinstance(data, (dict, list, tuple, str, bytes, bytearray)):
        return None
    positions = []
    for stream in streams:
        try:
            if not callable(getattr(stream, 'seek', None)) or not getattr(stream, 'seekable', lambda: True)():
                return None
            position = stream.tell()
            if not isinstance(position, int) or position < 0:
                return None
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        positions.append((stream, position))
    return positions


def _rewind_body(positions):
    try:
        if positions is None:
            raise ValueError('body is not replayable')
        for stream, position in positions:
            stream.seek(position)
            if stream.tell() != position:
                raise ValueError('body could not be rewound')
    except (AttributeError, OSError, TypeError, ValueError):
        raise RuntimeError('Workshop authentication expired. Please reopen the request source and rerun the command.') from None


class GalileoApiError(RuntimeError):
    def __init__(self, method: str, url: str, status_code: int, text: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.text = text
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {text[:400]}")


class GalileoClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GALILEO_API_KEY", "").strip()
        self.base_url = (base_url or os.environ.get("GALILEO_API_BASE_URL") or "https://api.galileo.ai").rstrip("/")
        self.last_response_headers: Dict[str, str] = {}
        self.session = None
        gateway = os.environ.get("GALILEO_LAB_GATEWAY_URL", "")

        if self.api_key:
            if not base_url and gateway and self.base_url == gateway:
                self.base_url = "https://api.multitenant.galileocloud.io"
            destination = urlsplit(self.base_url)
            if ((destination.hostname in SERVICE_HOSTS and destination.path.startswith(SERVICE_PATH))
                    or destination.hostname == "galileo-gateway.barrysecure.com"
                    or (gateway and self.base_url == gateway)):
                raise RuntimeError("Use a native Galileo API destination with GALILEO_API_KEY.")
            return

        if os.environ.get("GALILEO_LAB_PROFILE") != "workshop":
            raise RuntimeError("GALILEO_API_KEY is required")
        self.session = session_from_env()
        if self.base_url != self.session.base_url:
            raise RuntimeError(SESSION_ERROR)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.session:
            parsed_path = urlsplit(path)
            segments = unquote(parsed_path.path).split("/")
            if (parsed_path.scheme or parsed_path.netloc or parsed_path.fragment
                    or not parsed_path.path.startswith("/v2/") or any(part in {".", ".."} for part in segments)):
                raise RuntimeError(SESSION_ERROR)
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers: Dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        if self.session:
            headers = {key: value for key, value in headers.items()
                if key.lower() not in {"galileo-api-key", "authorization", "proxy-authorization"}}
            headers["Galileo-API-Key"] = self.session.token
            kwargs["allow_redirects"] = False
            kwargs.pop("auth", None)
            if self.session.base_url == BARRY_PROXY:
                kwargs["auth"] = NoAutomaticAuth()
        else:
            headers.setdefault("Galileo-API-Key", self.api_key)

        timeout = kwargs.pop("timeout", 30)
        parts = path.strip("/").split("/")
        telemetry_path = len(parts) >= 4 and parts[:2] == ["v2", "projects"] and parts[3] in {"traces", "spans", "sessions"}
        if "json" in kwargs and telemetry_path:
            kwargs["json"] = api_payload(kwargs["json"])
        positions = _body_positions(kwargs.get("files"), kwargs.get("data")) if self.session else []
        for attempt in range(2):
            try:
                response = requests.request(method, url, headers=dict(headers), timeout=timeout, **kwargs)
            except requests.RequestException:
                if self.session:
                    raise RuntimeError("Workshop request did not complete. Check findings before repeating a write.") from None
                raise
            if not self.session or response.status_code != 401 or attempt:
                break
            # Requests consumes file handles while preparing multipart uploads.
            _rewind_body(positions)
            renewal_env = dict(os.environ)
            if self.session.base_url == BARRY_PROXY:
                renewed = renew_barrysecure_session(self.session, renewal_env)
            else:
                try:
                    renewal_base = bootstrap_url(renewal_env).removesuffix("/sessions") + "/proxy"
                except ValueError:
                    raise RuntimeError(SESSION_ERROR) from None
                if renewal_base != self.base_url:
                    raise RuntimeError(SESSION_ERROR)
                renewed = bootstrap_session(renewal_env)
            if renewed.base_url != self.base_url:
                raise RuntimeError(SESSION_ERROR)
            self.session = renewed
            headers["Galileo-API-Key"] = renewed.token
        if self.session and 300 <= response.status_code < 400:
            raise RuntimeError(SESSION_ERROR)
        self.last_response_headers = dict(response.headers)
        if response.status_code >= 400:
            text = "Workshop request failed." if self.session else response.text
            raise GalileoApiError(method.upper(), url, response.status_code, text)

        if not response.text.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)
