"""Model providers: the OpenRouter client and a deterministic MockProvider.

The provider boundary is where *infrastructure* failures are distinguished from
*model* failures. A ProviderError means the run is recorded with a non-null
``error`` and excluded from statistics. Anything the model actually said — however
wrong or malformed — is returned as a normal response and scored as play.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from .prompts import BOARD_PREFIX, RESPONSE_JSON_SCHEMA
from .puzzles import Puzzle, canonical

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY_ENV = "OPENROUTER_API_KEY"


class ProviderError(RuntimeError):
    """Infrastructure failure: network, auth, rate limit exhaustion, malformed API reply."""


@dataclass
class ModelConfig:
    slug: str
    openrouter_id: str
    temperature: float = 0.0
    max_tokens: int = 4096
    attempts_per_day: int = 1
    max_cost_usd_per_run: float = 0.50
    json_schema: bool = True  # request strict response_format where supported
    reasoning: dict[str, Any] | None = None
    provider_prefs: dict[str, Any] | None = None  # passed through as OpenRouter "provider"

    @classmethod
    def from_yaml_entry(cls, entry: dict, defaults: dict) -> "ModelConfig":
        merged = {**defaults, **entry}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in merged.items() if k in known})


def load_models(path: str | Path) -> list[ModelConfig]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = doc.get("defaults") or {}
    models = [ModelConfig.from_yaml_entry(m, defaults) for m in doc.get("models") or []]
    slugs = [m.slug for m in models]
    if len(set(slugs)) != len(slugs):
        raise ValueError(f"duplicate model slugs in {path}")
    return models


@dataclass
class ProviderResponse:
    text: str
    model: str | None = None  # resolved model string from the response body
    reasoning_text: str | None = None  # OpenRouter returns thinking separately
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    finish_reason: str | None = None


class Provider(Protocol):
    name: str

    def complete(self, messages: list[dict], cfg: ModelConfig) -> ProviderResponse: ...


# --------------------------------------------------------------------------- mock


def _board_from_messages(messages: list[dict]) -> list[str]:
    """Read the current board back out of the most recent user message."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        for line in str(m.get("content", "")).splitlines():
            if line.startswith(BOARD_PREFIX):
                return [canonical(w) for w in line[len(BOARD_PREFIX):].split(",") if w.strip()]
    raise ProviderError("mock could not find a board in the conversation")


class MockProvider:
    """Deterministic, cost-free stand-in for a model.

    Behaviours (all deterministic, so run records are byte-reproducible):

    - ``perfect``  solves the puzzle in four guesses, groups in archive order.
    - ``naive``    guesses four adjacent words from the remaining board, sliding
                   the window each turn so it never repeats. Almost always loses
                   in four straight mistakes; exercises the loss path.
    - ``messy``    exercises every code path: a fenced response, a one-away guess,
                   prose with embedded JSON, an unparseable reply that triggers a
                   retry, an off-board word, then solves the rest.
    - ``scripted`` returns a fixed list of raw strings, then raises ProviderError.
    - ``broken``   raises ProviderError immediately (infrastructure-failure path).

    ``perfect`` and ``messy`` need the puzzle; the others read the board from the
    conversation like a real model would.
    """

    name = "mock"
    BEHAVIOURS = ("perfect", "naive", "messy", "scripted", "broken")

    def __init__(
        self,
        behaviour: str = "perfect",
        *,
        puzzle: Puzzle | None = None,
        script: list[str] | None = None,
    ):
        if behaviour not in self.BEHAVIOURS:
            raise ValueError(f"unknown mock behaviour {behaviour!r}")
        if behaviour in ("perfect", "messy") and puzzle is None:
            raise ValueError(f"mock behaviour {behaviour!r} needs the puzzle")
        self.behaviour = behaviour
        self.puzzle = puzzle
        self.script = list(script or [])
        self.calls = 0

    @staticmethod
    def _json(words, reasoning="mock") -> str:
        return json.dumps({"reasoning": reasoning, "guess": list(words)})

    def _next_group(self, board: list[str]):
        assert self.puzzle is not None
        remaining = set(board)
        for g in self.puzzle.groups:
            if set(g.members) <= remaining:
                return g
        raise ProviderError("mock: no unsolved group fits the board")

    def complete(self, messages: list[dict], cfg: ModelConfig) -> ProviderResponse:
        self.calls += 1
        if self.behaviour == "broken":
            raise ProviderError("mock provider is configured to fail")
        if self.behaviour == "scripted":
            if not self.script:
                raise ProviderError("mock script exhausted")
            return ProviderResponse(text=self.script.pop(0), model="mock/scripted", latency_ms=1)

        board = _board_from_messages(messages)
        if self.behaviour == "naive":
            k = (self.calls - 1) % max(1, len(board) - 3)
            text = self._json(board[k:k + 4], f"words {k + 1}-{k + 4} of the board")
        elif self.behaviour == "perfect":
            text = self._json(self._next_group(board).members, "I know this one")
        else:
            text = self._messy(board)
        return ProviderResponse(
            text=text,
            model=f"mock/{self.behaviour}",
            prompt_tokens=len(" ".join(str(m.get("content", "")) for m in messages)) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=1,
        )

    def _messy(self, board: list[str]) -> str:
        assert self.puzzle is not None
        g = self._next_group(board)
        n = self.calls
        if n == 1:  # fenced JSON, lowercase words -> still correct
            return "```json\n" + self._json([w.lower() for w in g.members], "fenced") + "\n```"
        if n == 2:  # one away: 3 from the next group + 1 from another
            other = next(w for w in board if w not in g.members)
            return self._json(list(g.members[:3]) + [other], "hmm")
        if n == 3:  # prose around JSON -> correct
            return "Let me think.\n\n" + self._json(g.members, "prose-wrapped") + "\n\nDone."
        if n == 4:  # unparseable -> retry
            return "I'm not sure, could you show the board again?"
        if n == 5:  # off-board word -> retry
            return self._json(list(g.members[:3]) + ["ZZYZX"], "off-board")
        return self._json(g.members, "recovered")


# --------------------------------------------------------------------------- openrouter

Transport = Callable[[dict, dict, float], tuple[int, dict]]


def _urllib_transport(payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = {"error": {"message": e.reason}}
        return e.code, body


@dataclass
class OpenRouterProvider:
    """OpenAI-compatible chat completions via OpenRouter, with capped exponential backoff."""

    api_key: str | None = None
    transport: Transport = _urllib_transport
    timeout: float = 600.0      # free-tier models queue behind paying traffic
    max_retries: int = 6
    backoff_base: float = 2.0
    backoff_cap: float = 120.0  # 2,4,8,16,32,64 -> ~2 min of patience before giving up
    sleep: Callable[[float], None] = time.sleep
    name: str = field(default="openrouter", init=False)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.environ.get(API_KEY_ENV)
        if not self.api_key:
            raise ProviderError(f"{API_KEY_ENV} is not set")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/AS489/ConnectionsBenchmark",
            "X-Title": "ConnectionsBench",
        }

    def build_payload(self, messages: list[dict], cfg: ModelConfig) -> dict:
        payload: dict[str, Any] = {
            "model": cfg.openrouter_id,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "usage": {"include": True},
        }
        if cfg.json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": RESPONSE_JSON_SCHEMA}
        if cfg.reasoning:
            payload["reasoning"] = dict(cfg.reasoning)
        if cfg.provider_prefs:
            payload["provider"] = dict(cfg.provider_prefs)
        return payload

    def complete(self, messages: list[dict], cfg: ModelConfig) -> ProviderResponse:
        payload = self.build_payload(messages, cfg)
        last_err = "unknown"
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                status, body = self.transport(payload, self._headers(), self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, body, last_err = 0, {}, f"transport: {e}"
            latency_ms = int((time.monotonic() - started) * 1000)

            if status == 200 and "choices" in body:
                # OpenRouter can return 200 with an error object in the body.
                if body.get("error"):
                    last_err = str(body["error"])
                    if self._retryable(body["error"].get("code", 500)):
                        self._wait(attempt)
                        continue
                    raise ProviderError(last_err)
                return self._parse_success(body, latency_ms)

            if status:
                last_err = f"HTTP {status}: {self._error_message(body)}"
            if status in (401, 403) or (400 <= status < 500 and status not in (408, 429)):
                raise ProviderError(last_err)
            self._wait(attempt)
        raise ProviderError(f"gave up after {self.max_retries + 1} attempts: {last_err}")

    @staticmethod
    def _retryable(code: Any) -> bool:
        try:
            code = int(code)
        except (TypeError, ValueError):
            return True
        return code in (408, 429) or code >= 500

    @staticmethod
    def _error_message(body: dict) -> str:
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err or body)[:500]

    def _wait(self, attempt: int) -> None:
        if attempt < self.max_retries:
            self.sleep(min(self.backoff_base ** attempt, self.backoff_cap))

    @staticmethod
    def _parse_success(body: dict, latency_ms: int) -> ProviderResponse:
        try:
            choice = body["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # some providers return content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content is None:
                content = ""
            # Reasoning models return their chain of thought in a sibling field.
            # Capture it: it is the artifact this benchmark exists to collect, and
            # when a model exhausts its budget it is the ONLY thing it produced.
            reasoning_text = message.get("reasoning")
            if not isinstance(reasoning_text, str):
                reasoning_text = None
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"malformed completion body: {e}") from None
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return ProviderResponse(
            text=str(content),
            reasoning_text=reasoning_text,
            model=body.get("model"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            cost_usd=float(usage.get("cost") or 0.0),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
        )


def redact(text: str) -> str:
    """Scrub anything that looks like an API key before it can reach a log or run record."""
    return re.sub(r"sk-or-v1-[A-Za-z0-9]{10,}", "sk-or-v1-***", text)
