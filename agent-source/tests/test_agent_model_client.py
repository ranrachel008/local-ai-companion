"""Offline, focused acceptance of the bounded MCP local-model demo client."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error


CLIENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "local_model_demo.py"
SPEC = importlib.util.spec_from_file_location("agent_local_model_demo", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


TOOLS = [
    {"name": name, "description": "Fake " + name, "inputSchema": {
        "type": "object", "properties": {"goal": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["goal"], "additionalProperties": False,
    }} for name in ("scout_search", "scout_research", "scout_plan", "scout_job")
]


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.events = []
        self.requests = []

    def models(self):
        self.events.append("GET /models")
        return {"data": [{"id": "observed-local-model"}]}

    def chat(self, body):
        self.events.append("POST /chat/completions")
        self.requests.append(json.loads(json.dumps(body)))
        return self.responses.pop(0)


def assistant(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}]}


def search_call(args=None, name="scout_search"):
    return {"id": "model-call-1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args or {"goal": "speech recognition", "limit": 2})}}


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.closed = False

    def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05"}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": json.dumps({
                "ok": True, "status": "SEARCHED", "data": {"job_id": "demo-job"}, "error": None})}], "isError": False}
        raise AssertionError(method)

    def notify(self, method):
        self.calls.append((method, None))

    def close(self):
        self.closed = True


class AgentModelClientTests(unittest.TestCase):
    def test_unavailable_model_never_launches_agent(self):
        client = demo.LocalModelClient()
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("[WinError 10061] actively refused")
        factory = mock.Mock()
        with mock.patch.object(demo.urllib.request, "build_opener", return_value=opener):
            evidence = demo.run_demo(client, ["not-launched.exe"], "Search only", mcp_factory=factory)
        self.assertEqual("FAIL", evidence["status"])
        self.assertEqual("MODEL_REQUEST_FAILED", evidence["errors"][0]["code"])
        self.assertIn("10061", evidence["errors"][0]["message"])
        factory.assert_not_called()
        self.assertEqual(1, opener.open.call_count)
        request = opener.open.call_args.args[0]
        self.assertEqual("GET", request.method)
        self.assertEqual("http://127.0.0.1:8080/v1/models", request.full_url)

    def test_no_tool_call_does_not_fabricate_search(self):
        model = FakeModel([assistant("Here are some projects without tool use.")])
        mcp = FakeMCP()
        evidence = demo.run_demo(model, ["fake-agent"], "Search only", mcp_factory=lambda command: mcp)
        self.assertEqual("FAIL", evidence["status"])
        self.assertEqual("NO_MODEL_TOOL_CALL", evidence["errors"][0]["code"])
        self.assertEqual([], evidence["tool_calls"])
        self.assertFalse(any(method == "tools/call" for method, _ in mcp.calls))
        self.assertTrue(mcp.closed)

    def test_successful_two_turn_fake_stdio_protocol(self):
        # This fake process is not the product, model service or a candidate.
        script = r'''
import json, sys
tools = json.loads(sys.argv[1])
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "notifications/initialized":
        initialized = True
        continue
    if method == "initialize":
        assert request["params"]["protocolVersion"] == "2024-11-05"
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
    elif method == "tools/list":
        assert initialized
        result = {"tools": tools}
    elif method == "tools/call":
        assert request["params"]["name"] == "scout_search"
        assert request["params"]["arguments"] == {"goal": "speech recognition", "limit": 2}
        envelope = {"ok": True, "status": "SEARCHED", "data": {"job_id": "fake-job", "candidates": [{"name": "fake-result"}]}, "error": None}
        result = {"content": [{"type": "text", "text": json.dumps(envelope)}], "isError": False}
    else:
        raise AssertionError(method)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''
        model = FakeModel([assistant(calls=[search_call()]), assistant("The saved search returned fake-result; no installation was run.")])
        order = []
        original_models = model.models
        def observed_models():
            order.append("models")
            return original_models()
        model.models = observed_models
        def factory(command):
            order.append("agent")
            return demo.MCPStdioClient(command, timeout=5)
        evidence = demo.run_demo(model, [sys.executable, "-u", "-c", script, json.dumps(TOOLS)],
                                 "Find speech recognition projects, search only.", mcp_factory=factory)
        self.assertEqual("PASS", evidence["status"], evidence["errors"])
        self.assertEqual(["models", "agent"], order)
        self.assertEqual(["GET /models", "POST /chat/completions", "POST /chat/completions"], model.events)
        self.assertEqual("observed-local-model", evidence["selected_model"])
        self.assertEqual(2, len(evidence["requests"]))
        self.assertEqual("auto", model.requests[0]["tool_choice"])
        self.assertFalse(model.requests[0]["stream"])
        self.assertEqual(["scout_search"], [tool["function"]["name"] for tool in model.requests[0]["tools"]])
        self.assertEqual("tool", model.requests[1]["messages"][-1]["role"])
        self.assertEqual("model-call-1", model.requests[1]["messages"][-1]["tool_call_id"])
        self.assertTrue(evidence["tool_calls"][0]["model_generated"])
        self.assertEqual("fake-job", json.loads(evidence["tool_calls"][0]["result"]["content"][0]["text"])["data"]["job_id"])

    def test_unobserved_explicit_model_never_launches_agent(self):
        model = FakeModel([])
        factory = mock.Mock()
        evidence = demo.run_demo(model, ["fake-agent"], "Search only", model_id="configured-but-unobserved", mcp_factory=factory)
        self.assertEqual("MODEL_NOT_OBSERVED", evidence["errors"][0]["code"])
        factory.assert_not_called()
        self.assertEqual([], model.requests)

    def test_disallowed_tool_and_unknown_fields_are_not_dispatched(self):
        for call in (search_call(name="scout_research"), search_call({"goal": "x", "confirm": True}), search_call({"goal": "x", "limit": True})):
            with self.subTest(call=call):
                model = FakeModel([assistant(calls=[call])])
                mcp = FakeMCP()
                evidence = demo.run_demo(model, ["fake-agent"], "Search only", mcp_factory=lambda command: mcp)
                self.assertEqual("FAIL", evidence["status"])
                self.assertFalse(any(method == "tools/call" for method, _ in mcp.calls))

    def test_tool_turns_are_bounded(self):
        second = search_call()
        second["id"] = "model-call-2"
        model = FakeModel([assistant(calls=[search_call()]), assistant(calls=[second])])
        mcp = FakeMCP()
        evidence = demo.run_demo(model, ["fake-agent"], "Search only", max_tool_turns=1, mcp_factory=lambda command: mcp)
        self.assertEqual("TOOL_TURN_LIMIT", evidence["errors"][0]["code"])
        self.assertEqual(1, sum(method == "tools/call" for method, _ in mcp.calls))

    def test_evidence_redacts_secret_fields_and_embedded_credentials(self):
        value = {"Authorization": "Bearer local-private-value", "nested": {"api_key": "key-secret", "password": "private-pass"},
                 "envelope": json.dumps({"data": {"api_key": "json-secret", "X-API-Key": "header-secret"}}),
                 "prose": 'Credentials: {"api_key": "inline-json-secret"}',
                 "text": "Bearer text-secret api_key=query-secret https://user:pass@localhost/v1 sk-privatevalue1234"}
        encoded = json.dumps(demo.sanitize(value))
        for secret in ("local-private-value", "key-secret", "private-pass", "json-secret", "header-secret",
                       "inline-json-secret", "text-secret", "query-secret", "user:pass", "sk-privatevalue1234"):
            self.assertNotIn(secret, encoded)
        self.assertEqual("[REDACTED]", json.loads(demo.sanitize(value)["envelope"])["data"]["api_key"])
        model = FakeModel([assistant("Authorization=hidden-value; api_key=embedded-value")])
        evidence = demo.run_demo(model, ["fake-agent"], "password=prompt-secret", mcp_factory=lambda command: FakeMCP())
        encoded_evidence = json.dumps(evidence)
        self.assertNotIn("hidden-value", encoded_evidence)
        self.assertNotIn("embedded-value", encoded_evidence)
        self.assertNotIn("prompt-secret", encoded_evidence)

    def test_nonlocal_and_credentialed_endpoints_rejected(self):
        for endpoint in ("https://cloud.example/v1", "http://user:pass@127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1?api_key=secret"):
            with self.subTest(endpoint=endpoint), self.assertRaises(demo.DemoError):
                demo.LocalModelClient(endpoint)

    def test_local_endpoint_redirect_is_rejected_without_fallback(self):
        request = demo.urllib.request.Request("http://127.0.0.1:8080/v1/models")
        with self.assertRaises(demo.DemoError) as error:
            demo._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://cloud.example/v1/models")
        self.assertEqual("MODEL_REDIRECT_REJECTED", error.exception.code)


if __name__ == "__main__":
    unittest.main()
