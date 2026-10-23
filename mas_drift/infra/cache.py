"""Дисковый кэш ответов LLM.

Ключ = sha256(provider, model, messages, tools, temperature, repeat_idx). repeat_idx обязателен:
иначе повторы одного и того же прогона получали бы один и тот же закэшированный ответ.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from mas_drift.infra.llm import LLMClient, LLMResponse, LLMWrapper


def cache_key(
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    repeat_idx: int,
    extra_params: dict | None = None,
) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "messages": messages,
        "tools": tools or [],
        "temperature": temperature,
        "repeat_idx": repeat_idx,
    }
    if extra_params:  # только если заданы — ключи без них не меняются
        payload["extra_params"] = extra_params
    blob = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> LLMResponse | None:
        path = self._path(key)
        if not path.exists():
            return None
        return LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, key: str, response: LLMResponse) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(response.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)


class CachedLLM(LLMWrapper):
    def __init__(self, inner: LLMClient, cache: DiskCache):
        super().__init__(inner)
        self.cache = cache

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *, repeat_idx: int = 0) -> LLMResponse:
        key = cache_key(self.provider, self.model, messages, tools, self.temperature, repeat_idx, self.extra_params)
        hit = self.cache.get(key)
        if hit is not None:
            return hit.model_copy(update={"cached": True, "api_requests": 0, "wait_s": 0.0})
        response = self.inner.chat(messages, tools, repeat_idx=repeat_idx)
        self.cache.put(key, response)
        return response
