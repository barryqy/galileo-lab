#!/usr/bin/env python3

import os
import time
import warnings
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import requests


PREFERRED_DEVNET_MODELS = ("gpt-4o", "gpt-5-mini", "gpt-5", "gpt-4")


class DevNetLlmError(RuntimeError):
    pass


def _model_ids(payload: Any) -> List[str]:
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    ids: List[str] = []
    for row in rows:
        if isinstance(row, str):
            ids.append(row)
        elif isinstance(row, dict):
            model_id = row.get("id") or row.get("name") or row.get("model")
            if model_id:
                ids.append(str(model_id))
    return ids


def _choose_model(model_ids: List[str], requested: Optional[str]) -> tuple[str, str]:
    requested = (requested or "").strip()
    if requested:
        return requested, "environment"

    for model in PREFERRED_DEVNET_MODELS:
        if model in model_ids:
            return model, "devnet-models"

    if model_ids:
        return model_ids[0], "devnet-models"

    raise DevNetLlmError("DevNet LLM proxy did not return any model IDs")


class DevNetLLM:
    def __init__(self, base_url: str, api_key: str, model: str, model_source: str, models: List[str]):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.model_source = model_source
        self.models = models

    @classmethod
    def from_env(cls) -> "DevNetLLM":
        base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
        api_key = os.environ.get("LLM_API_KEY", "").strip()
        requested_model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or os.environ.get("MODEL_NAME")
        )

        if not base_url or not api_key:
            raise DevNetLlmError("LLM_BASE_URL and LLM_API_KEY must come from the DevNet lab image")

        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        try:
            response = requests.get(f"{base_url}/models", headers=headers, timeout=30)
            response.raise_for_status()
            models = _model_ids(response.json())
        except Exception as exc:
            if requested_model:
                models = [requested_model]
            else:
                raise DevNetLlmError(f"Could not read DevNet LLM models from {base_url}/models: {exc}") from exc

        model, source = _choose_model(models, requested_model)
        return cls(base_url, api_key, model, source, models)

    def complete(self, messages: List[Dict[str, str]], *, max_tokens: int = 220) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        tried: List[str] = []
        fallback_models = [model for model in PREFERRED_DEVNET_MODELS if model in self.models and model != self.model]
        fallback_models += [model for model in self.models if model not in fallback_models and model != self.model]

        last_error: Optional[Exception] = None
        for model in [self.model] + fallback_models[:4]:
            tried.append(model)
            for attempt in range(3):
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                }
                try:
                    response = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=60)
                    response.raise_for_status()
                    data = response.json()
                    self.model = model
                    return data["choices"][0]["message"]["content"]
                except requests.HTTPError as exc:
                    last_error = exc
                    status = exc.response.status_code if exc.response is not None else None
                    if status not in (429, 500, 502, 503, 504):
                        raise
                    retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else attempt + 1
                    time.sleep(min(delay, 5))
                except Exception as exc:
                    last_error = exc
                    time.sleep(attempt + 1)

        raise DevNetLlmError(f"DevNet LLM calls failed for models: {', '.join(tried)}") from last_error


class BarryBot:
    system_prompt = (
        "You are BarryBot, a concise DevNet lab assistant. Help learners understand "
        "AI evaluation, observability, and runtime controls. Refuse requests that ask "
        "for credentials, secrets, private data, or unsafe bypass instructions. "
        "Answer in no more than two short sentences."
    )

    def __init__(self, llm: DevNetLLM):
        self.llm = llm

    @staticmethod
    def fallback_answer(prompt: str) -> str:
        text = prompt.lower()
        if "credential" in text or "secret" in text or "private" in text:
            return "I cannot help expose credentials or private data. Use approved recovery or rotation steps and keep sensitive values out of prompts and logs."
        if "production" in text or "watch" in text:
            return "Start with trace quality, latency, cost, and safety signals in Galileo. Then compare those runtime traces against evaluation datasets before changing the assistant."
        if "evaluate" in text or "dataset" in text:
            return "Use datasets and experiments to compare prompt and model changes against stable examples. Promote changes only when the scores and reviewed traces support the release."
        return "Use Galileo to connect evaluations, runtime traces, scorers, and human review so BarryBot behavior can be measured and improved."

    def ask(self, prompt: str) -> str:
        try:
            return self.llm.complete(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=96,
            )
        except DevNetLlmError:
            return self.fallback_answer(prompt)
