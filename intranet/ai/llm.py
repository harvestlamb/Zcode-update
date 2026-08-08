"""OpenAI-compatible LLM client for the ZCode mirror assistant.

Talks to a locally-deployed or cloud model (Ollama / vLLM / DeepSeek / GLM / ...)
via the /v1/chat/completions endpoint.

Model profiles (base URL, model name, API key) come from /config/llm.json and
can be switched at runtime via the admin UI. Temperature / max_tokens / timeout
are fixed internal defaults from the environment — not exposed in the UI.

Uses only the standard library so the container has no extra runtime deps.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Optional

import config_store

log = logging.getLogger("zcode-ai.llm")

DEFAULT_BASE = "http://host.docker.internal:11434/v1"
DEFAULT_MODEL = "glm-4-flash"


class LLMClient:
    def __init__(self):
        self._load_settings()

    def _env_defaults(self) -> dict:
        return {
            "base_url": (os.environ.get("LLM_BASE_URL") or DEFAULT_BASE).rstrip("/"),
            "model": os.environ.get("LLM_MODEL") or DEFAULT_MODEL,
            "api_key": os.environ.get("LLM_API_KEY") or "not-required",
            "label": os.environ.get("LLM_MODEL") or DEFAULT_MODEL,
            "id": "",
        }

    def _load_settings(self) -> None:
        """Active profile from llm.json, falling back to .env factory defaults."""
        defaults = self._env_defaults()
        active = config_store.get_model()
        if active:
            self.model_id = active["id"]
            self.label = active.get("label") or active["model"]
            self.base_url = str(active["base_url"]).rstrip("/")
            self.model = str(active["model"])
            self.api_key = str(active.get("api_key") or "") or "not-required"
        else:
            self.model_id = defaults["id"]
            self.label = defaults["label"]
            self.base_url = defaults["base_url"]
            self.model = defaults["model"]
            self.api_key = defaults["api_key"]

        # Internal knobs — not managed per-model in the admin UI.
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0.3"))
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "1024"))
        self.timeout = int(os.environ.get("LLM_TIMEOUT", "60"))

    def reload(self) -> None:
        self._load_settings()
        log.info(
            "LLM settings reloaded: id=%s model=%s base=%s",
            self.model_id, self.model, self.base_url,
        )

    def resolve(self, model_id: Optional[str] = None) -> dict:
        """Resolve a profile for chat/test. Falls back to active, then env."""
        if model_id:
            m = config_store.get_model(model_id)
            if not m:
                raise KeyError(f"未知模型: {model_id}")
        else:
            m = config_store.get_model()
        if m:
            return {
                "id": m["id"],
                "label": m.get("label") or m["model"],
                "base_url": str(m["base_url"]).rstrip("/"),
                "model": str(m["model"]),
                "api_key": str(m.get("api_key") or "") or "not-required",
            }
        d = self._env_defaults()
        return {
            "id": d["id"],
            "label": d["label"],
            "base_url": d["base_url"],
            "model": d["model"],
            "api_key": d["api_key"],
        }

    def public_models(self) -> list:
        """Profiles safe to expose to the chat widget (no API keys)."""
        store = config_store.list_models(mask_keys=True)
        return [
            {
                "id": m["id"],
                "label": m["label"],
                "model": m["model"],
                "active": m["active"],
            }
            for m in store["models"]
        ]

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate when the provider omits usage (CJK≈1, ASCII≈/4)."""
        if not text:
            return 0
        cjk = 0
        other = 0
        for ch in text:
            o = ord(ch)
            if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
                cjk += 1
            else:
                other += 1
        return max(1, cjk + (other + 3) // 4)

    @classmethod
    def _usage_from_response(cls, out: dict, system: str, user: str, content: str) -> dict:
        usage = out.get("usage") or {}
        try:
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            prompt = completion = total = 0
        estimated = False
        if prompt <= 0 and completion <= 0 and total <= 0:
            prompt = cls.estimate_tokens(system) + cls.estimate_tokens(user)
            completion = cls.estimate_tokens(content)
            total = prompt + completion
            estimated = True
        elif total <= 0:
            total = prompt + completion
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "estimated": estimated,
        }

    def chat(self, system: str, user: str, model_id: Optional[str] = None) -> dict:
        """Single-turn completion.

        Returns {"content": str, "usage": {prompt_tokens, completion_tokens,
        total_tokens, estimated}}.
        """
        cfg = self.resolve(model_id)
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{cfg['base_url']}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['api_key']}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        content = out["choices"][0]["message"]["content"].strip()
        return {
            "content": content,
            "usage": self._usage_from_response(out, system, user, content),
        }

    def ping(self, base_url: str) -> bool:
        try:
            base = base_url.replace("/v1", "")
            req = urllib.request.Request(base + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status < 500
        except Exception:  # noqa: BLE001
            return False

    def test_connection(self, model_id: Optional[str] = None, draft: Optional[dict] = None) -> dict:
        """Probe /v1/models (or Ollama /health) for a saved profile or draft form."""
        if draft:
            cfg = {
                "base_url": str(draft.get("base_url") or "").rstrip("/"),
                "model": str(draft.get("model") or ""),
                "api_key": str(draft.get("api_key") or "") or "not-required",
                "label": draft.get("label") or draft.get("model") or "",
                "id": draft.get("id") or "",
            }
            if not cfg["api_key"] or cfg["api_key"] == "not-required":
                # When testing an edit form with blank key, reuse saved key if any.
                if draft.get("id"):
                    saved = config_store.get_model(str(draft["id"]))
                    if saved and saved.get("api_key"):
                        cfg["api_key"] = saved["api_key"]
        else:
            cfg = self.resolve(model_id)

        result = {
            "ok": False,
            "models": [],
            "error": "",
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "label": cfg.get("label") or cfg["model"],
        }
        if not cfg["base_url"]:
            result["error"] = "服务地址为空"
            return result

        try:
            req = urllib.request.Request(
                f"{cfg['base_url']}/models",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            ids = []
            for item in payload.get("data", []):
                mid = item.get("id") or item.get("name")
                if mid:
                    ids.append(mid)
            result["ok"] = True
            result["models"] = ids[:50]
            if cfg["model"] and ids and cfg["model"] not in ids:
                result["error"] = (
                    f"模型列表里没有「{cfg['model']}」，可用: "
                    f"{', '.join(ids[:10]) or '(空)'}"
                )
            return result
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"/v1/models 不可用: {exc}"

        if self.ping(cfg["base_url"]):
            result["ok"] = True
            result["error"] = ""
            return result
        return result
