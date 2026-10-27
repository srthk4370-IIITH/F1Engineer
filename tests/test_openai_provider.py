"""Exercise the real OpenAIProvider + Agent tool loop against a fake OpenAI-compatible streaming server.

Verifies: streamed tool-call deltas (name and arguments split across chunks) are reassembled, the tool runs,
its result goes back to the model, and the final answer streams out as sentences.
"""
import json
import socket
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
from fastapi.responses import StreamingResponse  # noqa: E402

SEEN: list[dict] = []


def _chunk(delta: dict, finish=None) -> str:
    body = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n"


def _app():
    app = fastapi.FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(req: dict):
        SEEN.append(req)
        msgs = req["messages"]

        def gen():
            if msgs[-1]["role"] == "user":
                yield _chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                              "function": {"name": "get_corner_history", "arguments": ""}}]})
                yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"corner": '}}]})
                yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Loop"}'}}]})
                yield _chunk({}, "tool_calls")
            else:
                data = json.loads(msgs[-1]["content"])
                text = f"At the {data['corner']} you lost {data['laps'][-1]['time_loss']} seconds. Brake later, be brave."
                for w in text.split(" "):
                    yield _chunk({"content": w + " "})
                    time.sleep(0.005)
                yield _chunk({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_openai_provider_tool_loop(monkeypatch):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{port}/v1")
    from f1engineer.llm.agent import Agent, OpenAIProvider

    calls = []

    def corner_history(corner: str):
        calls.append(corner)
        return {"corner": corner, "laps": [{"lap": 3, "time_loss": 0.2, "causes": ["coasted 52 m"]}]}

    agent = Agent({"get_corner_history": corner_history}, lambda: "track=Silverstone", sass=2, provider=OpenAIProvider("fake"))
    t0 = time.perf_counter()
    out = list(agent.ask("What happened at the Loop?"))
    server.should_exit = True
    assert calls == ["Loop"]
    assert out[0].startswith("At the Loop you lost 0.2 seconds.")
    assert len(SEEN) == 2 and SEEN[0]["stream"] is True and SEEN[0]["tools"]
    assert SEEN[1]["messages"][-1]["role"] == "tool" and SEEN[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "get_corner_history"
    assert time.perf_counter() - t0 < 5
