"""Model adapters.

GeminiLLM talks to Google's Gemini API with the server's key. ScriptedLLM replays a fixed
solution for the built-in demos so the whole workflow works with no key at all; runs using
it are labelled *simulated* everywhere.

Conversation format used by the agent (provider neutral):
  {"role": "user", "content": "text"}
  {"role": "assistant", "content": [text / tool_use blocks], "raw": <provider object or None>}
  {"role": "user", "content": [tool_result blocks]}
`raw` lets an adapter replay the model's own response byte-for-byte, which Gemini 3 requires
(thought signatures must be returned exactly as received during function calling).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .secrets_util import redact


class LLMError(Exception):
    """A model-side failure with a message that is safe to show to any visitor."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind   # auth | rate_limit | model | network | blocked | error


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    raw: Any = field(default=None, repr=False)

    def assistant_content(self) -> list[dict]:
        blocks: list[dict] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for c in self.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
        return blocks


class GeminiLLM:
    simulated = False

    def __init__(self, api_key: str, model: str, *, thinking_level: str = "", max_output_tokens: int = 8192,
                 http_client: Any = None, client: Any = None, retry_attempts: int = 3):
        from google import genai
        from google.genai import types
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.thinking_level = thinking_level
        self._api_key = api_key
        self._types = types
        if client is not None:
            self.client = client
        else:
            self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
                timeout=90_000, httpx_client=http_client,
                retry_options=types.HttpRetryOptions(attempts=retry_attempts, initial_delay=1.0, max_delay=8.0)))

    # -- errors ------------------------------------------------------------
    def _translate(self, exc: Exception) -> LLMError:
        name = type(exc).__name__
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "")
        detail = redact(str(getattr(exc, "message", "") or exc), self._api_key)[:300]
        low = detail.lower()
        if "host not in allowlist" in low or "host_not_allowed" in low or "egress" in low:
            return LLMError("This server's network policy blocks access to the Gemini API (generativelanguage.googleapis.com). "
                            "The site owner needs to allow that host.", "network")
        if "api key not valid" in low or "api_key_invalid" in low or code in (401,):
            return LLMError("The AI service rejected this server's API key. The site owner needs to update GEMINI_API_KEY.", "auth")
        if code == 403 or status == "PERMISSION_DENIED":
            return LLMError("This server's Gemini key is not permitted to use this model or region. The site owner needs to check it.", "auth")
        if code == 404 or status == "NOT_FOUND":
            return LLMError(f"Model {self.model!r} was not found. The site owner needs to set GEMINI_MODEL to a model that exists.", "model")
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            return LLMError("The AI service is busy or its quota is used up. Wait a minute and try again.", "rate_limit")
        if code is not None and code >= 500:
            return LLMError("The AI service had a temporary problem. Try again in a moment.", "error")
        if code == 400:
            return LLMError(f"The AI service rejected the request: {detail}", "error")
        if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException", "APIConnectionError") or "timed out" in low \
                or "connect" in name.lower():
            return LLMError("Could not reach the AI service. Try again in a moment.", "network")
        return LLMError(f"Model call failed ({name}): {detail}", "error")

    def check(self) -> None:
        """Cheap validation call. Raises LLMError."""
        try:
            self.client.models.generate_content(
                model=self.model, contents="ping",
                config=self._types.GenerateContentConfig(max_output_tokens=16))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None

    # -- conversion --------------------------------------------------------
    def _tools(self, tools: list[dict]):
        t = self._types
        decls = []
        for spec in tools:
            schema = spec.get("input_schema") or {}
            kwargs = {"name": spec["name"], "description": spec.get("description", "")}
            if schema.get("properties"):          # Gemini rejects an empty OBJECT schema
                kwargs["parameters_json_schema"] = schema
            decls.append(t.FunctionDeclaration(**kwargs))
        return [t.Tool(function_declarations=decls)]

    def _contents(self, messages: list[dict]) -> list:
        t = self._types
        names: dict[str, str] = {}
        out: list = []

        def add(role: str, parts: list) -> None:
            if not parts:
                return
            if out and out[-1].role == role:       # Gemini prefers strictly alternating turns
                out[-1] = t.Content(role=role, parts=[*(out[-1].parts or []), *parts])
            else:
                out.append(t.Content(role=role, parts=parts))

        for m in messages:
            content = m["content"]
            if m["role"] == "assistant":
                blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
                for b in blocks:
                    if b.get("type") == "tool_use":
                        names[b["id"]] = b["name"]
                raw = m.get("raw")
                if raw is not None and getattr(raw, "parts", None):
                    out.append(raw) if not (out and out[-1].role == "model") else add("model", list(raw.parts))
                    continue
                parts = []
                for b in blocks:
                    if b.get("type") == "text" and b.get("text"):
                        parts.append(t.Part(text=b["text"]))
                    elif b.get("type") == "tool_use":
                        parts.append(t.Part(function_call=t.FunctionCall(name=b["name"], args=b.get("input") or {})))
                add("model", parts)
            else:
                if isinstance(content, str):
                    add("user", [t.Part(text=content)])
                    continue
                parts = []
                for b in content:
                    if b.get("type") == "tool_result":
                        parts.append(t.Part.from_function_response(
                            name=names.get(b["tool_use_id"], "tool"), response={"result": b.get("content", "")}))
                    elif b.get("type") == "text":
                        parts.append(t.Part(text=b["text"]))
                add("user", parts)
        return out

    # -- main call ---------------------------------------------------------
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        t = self._types
        cfg = dict(system_instruction=system, tools=self._tools(tools), max_output_tokens=self.max_output_tokens,
                   automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        if self.thinking_level:
            cfg["thinking_config"] = t.ThinkingConfig(thinking_level=self.thinking_level)
        try:
            resp = self.client.models.generate_content(model=self.model, contents=self._contents(messages),
                                                       config=t.GenerateContentConfig(**cfg))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None

        usage = getattr(resp, "usage_metadata", None)
        tin = int(getattr(usage, "prompt_token_count", 0) or 0)
        tout = int(getattr(usage, "candidates_token_count", 0) or 0) + int(getattr(usage, "thoughts_token_count", 0) or 0)

        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            raise LLMError(f"The AI service declined this request ({getattr(reason, 'name', reason) or 'no response'}).", "blocked")
        cand = candidates[0]
        finish = getattr(getattr(cand, "finish_reason", None), "name", str(getattr(cand, "finish_reason", "") or ""))
        content = getattr(cand, "content", None)
        parts = list(getattr(content, "parts", None) or [])

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, part in enumerate(parts):
            if getattr(part, "thought", False):
                continue
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append(ToolCall(id=getattr(fc, "id", None) or f"gemini_{len(messages)}_{i}", name=fc.name,
                                      input=dict(fc.args or {})))
            elif getattr(part, "text", None):
                text_parts.append(part.text)
        return LLMResponse(text="\n".join(text_parts).strip(), tool_calls=calls, input_tokens=tin, output_tokens=tout,
                           stop_reason=finish or ("tool_use" if calls else "end_turn"), raw=content if parts else None)


# ----------------------------------------------------------------------------
# Scripted model for the built-in demos
# ----------------------------------------------------------------------------
ScriptStep = tuple[str, list[tuple[str, dict]]]   # (assistant text, [(tool, args), ...])


class ScriptedLLM:
    """Replays a fixed sequence of assistant turns. Token counts are *estimates*."""
    simulated = True
    model = "scripted-demo"

    def __init__(self, script: list[ScriptStep]):
        self.script = script
        self._turn = 0

    def check(self) -> None:  # pragma: no cover - trivial
        return None

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if self._turn >= len(self.script):
            return LLMResponse(text="Script finished.", tool_calls=[], stop_reason="end_turn")
        text, calls = self.script[self._turn]
        self._turn += 1
        approx_in = len(system) // 4 + sum(len(json.dumps(m, default=str)) for m in messages) // 4
        tool_calls = [ToolCall(id=f"scripted_{self._turn}_{i}", name=n, input=a) for i, (n, a) in enumerate(calls)]
        approx_out = (len(text) + sum(len(json.dumps(a)) for _, a in calls)) // 4
        return LLMResponse(text=text, tool_calls=tool_calls, input_tokens=approx_in, output_tokens=approx_out,
                           stop_reason="tool_use" if tool_calls else "end_turn")


def estimate_cost(input_tokens: int, output_tokens: int, price_in: float, price_out: float) -> float:
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
