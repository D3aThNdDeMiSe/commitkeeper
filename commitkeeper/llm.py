"""Local LLM client (Ollama). All calls return schema-constrained JSON and are logged
with latency and token counts so model usage can be reported."""
from __future__ import annotations

import json
import time

import requests

from .config import Config
from .logbook import Logbook


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: Config, log: Logbook):
        self.cfg = cfg
        self.log = log
        self.calls = 0

    def json_call(self, purpose: str, system: str, user: str, schema: dict, think: bool = False) -> dict:
        self.calls += 1
        fail = self.cfg.simulate_failure
        if fail.startswith("llm_after:") and self.calls > int(fail.split(":")[1]):
            self.log.event("llm_call", purpose, {"error": "simulated LLM outage"}, outcome="error")
            raise LLMError("simulated LLM outage (CK_SIMULATE_FAILURE)")

        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": think,
            "format": schema,
            "options": {"temperature": 0, "seed": self.cfg.seed, "num_ctx": self.cfg.num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        last_error = ""
        for attempt in (1, 2):
            start = time.perf_counter()
            try:
                resp = requests.post(f"{self.cfg.ollama_url}/api/chat", json=payload,
                                     timeout=self.cfg.llm_timeout_s)
                resp.raise_for_status()
                data = resp.json()
                parsed = json.loads(data["message"]["content"])
            except (requests.RequestException, KeyError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.log.event("llm_call", purpose, {"attempt": attempt, "error": last_error}, outcome="error",
                               duration_ms=int((time.perf_counter() - start) * 1000))
                continue
            self.log.event("llm_call", purpose, {
                "model": self.cfg.model, "attempt": attempt, "think": think,
                "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                "prompt": user, "thinking": (data["message"].get("thinking") or "")[:2000], "response": parsed,
            }, duration_ms=int((time.perf_counter() - start) * 1000))
            return parsed
        raise LLMError(f"LLM call '{purpose}' failed twice: {last_error}")
