"""A strict stand-in for the Gemini API, for trying the live path without a key. NOT for production.

Usage:  python scripts/fake_gemini.py          (listens on 127.0.0.1:9199)
        GEMINI_API_KEY=fake-key GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:9199 python run.py
Then open the app, pick the pagination demo and choose "Live AI".

Plays the pagination demo's solution as function calls, and behaves like Gemini 3 in one important way:
if a replayed model turn is missing the thought signature it issued, it answers 400."""
import base64, json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.demo.tasks import DEMOS

SCRIPT = DEMOS["pagination"].script
SEEN = []

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send(200, {"seen": SEEN})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append({"path": self.path, "key": self.headers.get("x-goog-api-key"), "tools": bool(body.get("tools"))})
        contents = body.get("contents", [])
        if not body.get("tools"):                                     # the key/status ping
            return self._send(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "pong"}]}, "finishReason": "STOP"}]})
        model_turns = [c for c in contents if c.get("role") == "model"]
        for c in model_turns:                                          # strict: signature must come back
            fcs = [p for p in c["parts"] if "functionCall" in p or "function_call" in p]
            if fcs and not any(("thoughtSignature" in p or "thought_signature" in p) for p in fcs):
                return self._send(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Function call is missing a thought_signature"}})
        n = len(model_turns)
        # every function response must line up with the previous model turn's calls
        if n and not any("functionResponse" in p or "function_response" in p for p in contents[-1]["parts"]):
            return self._send(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "expected function response"}})
        if n >= len(SCRIPT):
            return self._send(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}, "finishReason": "STOP"}]})
        text, calls = SCRIPT[n]
        parts = ([{"text": text}] if text else [])
        for i, (name, args) in enumerate(calls):
            fc = {"functionCall": {"name": name, "args": args}}
            if i == 0: fc["thoughtSignature"] = base64.b64encode(f"sig-{n}".encode()).decode()
            parts.append(fc)
        self._send(200, {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
                         "usageMetadata": {"promptTokenCount": 800 + 400 * n, "candidatesTokenCount": 60, "thoughtsTokenCount": 40}})

if __name__ == "__main__":
    print("Fake Gemini listening on http://127.0.0.1:9199")
    HTTPServer(("127.0.0.1", 9199), H).serve_forever()
