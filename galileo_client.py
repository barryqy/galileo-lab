#!/usr/bin/env python3

import json
import os
import warnings
from typing import Any, Dict, Optional

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import requests


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

        if not self.api_key:
            raise RuntimeError("GALILEO_API_KEY is required")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers: Dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Galileo-API-Key", self.api_key)

        timeout = kwargs.pop("timeout", 30)
        parts = path.strip("/").split("/")
        telemetry_path = len(parts) >= 4 and parts[:2] == ["v2", "projects"] and parts[3] in {"traces", "spans", "sessions"}
        if "json" in kwargs and telemetry_path:
            kwargs["json"] = api_payload(kwargs["json"])
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        self.last_response_headers = dict(response.headers)
        if response.status_code >= 400:
            raise GalileoApiError(method.upper(), url, response.status_code, response.text)

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
