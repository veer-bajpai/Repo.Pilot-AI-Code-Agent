"""The Gemini adapter is exercised with the REAL google-genai SDK against a fake Google server
(httpx MockTransport), so request building, response parsing and error mapping are all real code paths."""
import base64
import json

import httpx
import pytest

from app.llm import GeminiLLM, LLMError, ScriptedLLM, estimate_cost
from app.tools import TOOL_SCHEMAS

KEY = "AIza" + "S" * 35
SIG = base64.b64encode(b"opaque-thought-signature").decode()


def llm_with(handler, **kw):
    kw.setdefault("retry_attempts", 1)
    return GeminiLLM(KEY, "gemini-3.8-flash", http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def ok(parts, usage=None, finish="STOP"):
    return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
                                     "usageMetadata": usage or {"promptTokenCount": 10, "candidatesTokenCount": 2}})


def err(status, message, st="INVALID_ARGUMENT"):
    return httpx.Response(status, json={"error": {"code": status, "message": message, "status": st}})


class TestRequestAndResponse:
    def test_parses_calls_and_counts_thinking_tokens_and_keeps_key_out_of_the_url(self):
        seen = []

        def handler(req):
            seen.append(req)
            return ok([{"text": "Looking around."},
                       {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": SIG}],
                      {"promptTokenCount": 120, "candidatesTokenCount": 30, "thoughtsTokenCount": 50})

        out = llm_with(handler).complete("SYSTEM PROMPT", [{"role": "user", "content": "task"}], TOOL_SCHEMAS)
        req = seen[0]
        body = json.loads(req.content)
        assert req.url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
        assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)
        assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM PROMPT"
        decls = {d["name"]: d for d in body["tools"][0]["functionDeclarations"]}
        assert set(decls) == {t["name"] for t in TOOL_SCHEMAS}
        assert not any(k.startswith("param") for k in decls["git_diff"])          # empty schemas are omitted
        assert any(k.startswith("param") for k in decls["read_file"])
        assert out.text == "Looking around." and [(c.name, c.input) for c in out.tool_calls] == [("read_file", {"path": "a.py"})]
        assert (out.input_tokens, out.output_tokens) == (120, 80)                 # thinking tokens bill as output

    def test_thinking_level_is_sent_only_when_configured(self):
        bodies = []
        llm_with(lambda r: (bodies.append(json.loads(r.content)), ok([{"text": "x"}]))[1], thinking_level="low") \
            .complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        llm_with(lambda r: (bodies.append(json.loads(r.content)), ok([{"text": "x"}]))[1]) \
            .complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        level = bodies[0]["generationConfig"]["thinkingConfig"]      # the SDK spells this field in snake_case
        assert (level.get("thinking_level") or level.get("thinkingLevel")).lower() == "low"
        assert "thinkingConfig" not in bodies[1].get("generationConfig", {})


class TestHistoryReplay:
    def _two_turns(self):
        sent = []

        def handler(req):
            sent.append(json.loads(req.content))
            if len(sent) == 1:
                return ok([{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": SIG}])
            return ok([{"text": "done"}])

        llm = llm_with(handler)
        msgs = [{"role": "user", "content": "task"}]
        r1 = llm.complete("s", msgs, TOOL_SCHEMAS)
        msgs += [{"role": "assistant", "content": r1.assistant_content(), "raw": r1.raw},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": r1.tool_calls[0].id, "content": "file text"}]},
                 {"role": "user", "content": "Continue using the tools."}]
        return llm, msgs, sent

    def test_thought_signature_is_returned_exactly_and_results_are_matched_to_names(self):
        llm, msgs, sent = self._two_turns()
        llm.complete("s", msgs, TOOL_SCHEMAS)
        contents = sent[1]["contents"]
        assert [c["role"] for c in contents] == ["user", "model", "user"]        # consecutive user turns merged
        assert SIG in json.dumps(contents)                                        # required by Gemini 3
        fr = next(p["functionResponse"] for c in contents for p in c["parts"] if "functionResponse" in p)
        assert fr["name"] == "read_file" and fr["response"] == {"result": "file text"}
        assert len(contents[-1]["parts"]) == 2

    def test_history_without_raw_is_rebuilt_from_blocks(self):
        sent = []
        llm = llm_with(lambda r: (sent.append(json.loads(r.content)), ok([{"text": "ok"}]))[1])
        msgs = [{"role": "user", "content": "task"},
                {"role": "assistant", "raw": None, "content": [{"type": "text", "text": "hm"},
                                                              {"type": "tool_use", "id": "x1", "name": "list_files", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x1", "content": "a.py"}]}]
        llm.complete("s", msgs, TOOL_SCHEMAS)
        parts = sent[0]["contents"][1]["parts"]
        assert parts[0]["text"] == "hm" and parts[1]["functionCall"]["name"] == "list_files"


class TestFailures:
    @pytest.mark.parametrize("status,message,st,kind,fragment", [
        (400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", "auth", "GEMINI_API_KEY"),
        (403, "Permission denied", "PERMISSION_DENIED", "auth", "not permitted"),
        (404, "models/nope is not found", "NOT_FOUND", "model", "GEMINI_MODEL"),
        (429, "Quota exceeded", "RESOURCE_EXHAUSTED", "rate_limit", "quota"),
        (500, "internal", "INTERNAL", "error", "temporary problem"),
        (400, "Request payload is too large", "INVALID_ARGUMENT", "error", "too large"),
    ])
    def test_http_errors_become_friendly_messages(self, status, message, st, kind, fragment):
        llm = llm_with(lambda r: err(status, message, st))
        with pytest.raises(LLMError) as ei:
            llm.complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        assert ei.value.kind == kind and fragment in str(ei.value)
        with pytest.raises(LLMError):
            llm.check()

    def test_egress_block_is_not_mistaken_for_a_bad_key(self):
        """Hosts with an outbound allowlist answer 403 for anything not on it; that is a network-policy
        problem, and telling the owner their key or region is wrong would send them debugging the wrong thing."""
        blocked = lambda r: httpx.Response(403, text="Host not in allowlist: generativelanguage.googleapis.com.")
        with pytest.raises(LLMError) as ei:
            llm_with(blocked).complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        assert ei.value.kind == "network" and "network policy" in str(ei.value) and "key" not in str(ei.value).lower()

    def test_network_failure(self):
        def boom(req):
            raise httpx.ConnectError("no route to host")
        with pytest.raises(LLMError) as ei:
            llm_with(boom).complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        assert ei.value.kind == "network"

    def test_a_key_echoed_by_the_upstream_error_is_redacted(self):
        llm = llm_with(lambda r: err(400, f"bad request from key {KEY} rejected"))
        with pytest.raises(LLMError) as ei:
            llm.complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        assert KEY not in str(ei.value)

    def test_blocked_prompt(self):
        llm = llm_with(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
        with pytest.raises(LLMError) as ei:
            llm.complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        assert ei.value.kind == "blocked" and "SAFETY" in str(ei.value)

    def test_empty_candidate_is_an_empty_turn_not_a_crash(self):
        """e.g. finishReason MALFORMED_FUNCTION_CALL comes back with no content; the agent nudges."""
        llm = llm_with(lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]}))
        out = llm.complete("s", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
        assert out.tool_calls == [] and out.text == "" and out.raw is None and "MALFORMED" in out.stop_reason


def test_scripted_llm_replays_and_estimates():
    llm = ScriptedLLM([("hi", [("list_files", {})]), ("", [("finish", {"summary": "x"})])])
    a, b = llm.complete("s", [{"role": "user", "content": "task"}], []), llm.complete("s", [], [])
    assert a.tool_calls[0].name == "list_files" and b.tool_calls[0].name == "finish" and a.input_tokens > 0
    assert llm.complete("s", [], []).tool_calls == [] and llm.simulated


def test_cost_estimate():
    assert estimate_cost(1_000_000, 1_000_000, 1.5, 7.5) == 9.0
