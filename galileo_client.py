#!/usr/bin/env python3

import os
import warnings
from typing import Any, Dict, Optional

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import requests


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

        if not self.api_key:
            raise RuntimeError("GALILEO_API_KEY is required")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers: Dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Galileo-API-Key", self.api_key)

        timeout = kwargs.pop("timeout", 30)
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
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
