#!/usr/bin/env bash
set -euo pipefail

: "${BASE_URL:=http://127.0.0.1:18087}"
: "${MODEL:=glm-5.3-flash}"
: "${CLIENT_TOKEN:?CLIENT_TOKEN is required}"
: "${TEST_WORKSPACE:=$(mktemp -d "${TMPDIR:-/tmp}/todo2api-tool-client.XXXXXX")}"

export BASE_URL MODEL CLIENT_TOKEN TEST_WORKSPACE

python3 - <<'PY'
import json
import os
import subprocess
import urllib.request
from pathlib import Path

base_url = os.environ["BASE_URL"].rstrip("/")
client_token = os.environ["CLIENT_TOKEN"]
model = os.environ["MODEL"]
workspace = Path(os.environ["TEST_WORKSPACE"]).resolve()
workspace.mkdir(parents=True, exist_ok=True)
fixture = workspace / "fixture.txt"
marker = "verified-local-tool-result-7391"
fixture.write_text(marker + "\n", encoding="utf-8")

tools = [
    {
        "type": "function",
        "function": {
            "name": "use_capability",
            "description": "Search the local capability catalog before selecting a tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search"]},
                    "query": {"type": "string"},
                },
                "required": ["action", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one UTF-8 file from the client workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a command in the client workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


def post_json(body, todo_id=""):
    """Send one non-streaming Chat Completions request."""
    request = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + client_token,
            "Content-Type": "application/json",
            **({"X-Todo2API-Todo-ID": todo_id} if todo_id else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=360) as response:
        return json.load(response)


def post_stream(body):
    """Send one streaming request and collect its semantic chunks."""
    request = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + client_token, "Content-Type": "application/json"},
    )
    chunks = []
    with urllib.request.urlopen(request, timeout=360) as response:
        for raw in response:
            line = raw.decode().strip()
            if line.startswith("data: {"):
                chunks.append(json.loads(line[6:]))
            elif line == "data: [DONE]":
                break
    return chunks


def execute(call):
    """Execute only the three bounded regression tools."""
    name = call["function"]["name"]
    arguments = json.loads(call["function"]["arguments"])
    if name == "use_capability":
        return json.dumps(
            {
                "query": arguments.get("query"),
                "results": [
                    {"capability_id": "tool:bash", "name": "bash", "status": "ready"},
                    {"capability_id": "tool:read_file", "name": "read_file", "status": "ready"},
                ],
                "note": "Local catalog search only; the requested MCP was not found.",
            },
            ensure_ascii=False,
        )
    if name == "read_file":
        path = Path(arguments["path"]).resolve()
        if workspace not in path.parents and path != workspace:
            raise AssertionError("read_file escaped TEST_WORKSPACE")
        return path.read_text(encoding="utf-8")
    if name == "bash":
        command = arguments["command"].strip()
        if str(fixture) not in command or not command.startswith("cat "):
            raise AssertionError("bash regression accepts only cat of the fixture")
        return subprocess.run(["cat", str(fixture)], check=True, capture_output=True, text=True).stdout
    raise AssertionError("unexpected tool: " + name)


def assert_clean(text):
    """Reject the two production regressions in user-visible text."""
    lowered = text.lower()
    blocked = ["todofor.ai", "todofor ai", "设备离线", "名称过期", "all tools are offline"]
    found = [term for term in blocked if term in lowered]
    if found:
        raise AssertionError("blocked reply content: " + ", ".join(found))


def complete(messages, todo_id="", required_tool=None):
    """Run a bounded client-side tool loop and return the final reply."""
    calls = []
    for _ in range(5):
        body = {"model": model, "messages": messages, "tools": tools, "stream": False}
        if todo_id:
            body["metadata"] = {"todo2api.todo_id": todo_id}
        response = post_json(body, todo_id)
        response_todo = response.get("metadata", {}).get("todo2api.todo_id", "")
        if todo_id and response_todo != todo_id:
            raise AssertionError("Todo ID changed during continuation")
        todo_id = response_todo or todo_id
        message = response["choices"][0]["message"]
        finish = response["choices"][0].get("finish_reason")
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            if finish != "tool_calls" or len(tool_calls) != 1:
                raise AssertionError("invalid tool-call termination")
            call = tool_calls[0]
            calls.append(call["function"]["name"])
            result = execute(call)
            messages.extend(
                [
                    message,
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["function"]["name"],
                        "content": result,
                    },
                ]
            )
            continue
        text = message.get("content") or ""
        assert_clean(text)
        if finish != "stop":
            raise AssertionError("final response did not stop normally")
        if required_tool and required_tool not in calls:
            raise AssertionError("required tool was not called: " + required_tool)
        return text, todo_id, calls
    raise AssertionError("tool loop exceeded five turns")


# Scenario 1: system/developer instructions plus a real file read.
basic_messages = [
    {"role": "system", "content": "Act as a concise coding assistant."},
    {"role": "developer", "content": "Use a client tool for local facts."},
    {"role": "user", "content": f"Use read_file to read {fixture} and quote its exact content."},
]
basic_text, _, basic_calls = complete(basic_messages, required_tool="read_file")
if marker not in basic_text:
    raise AssertionError("final answer omitted the real file result")
print("PASS openai_nonstream", ",".join(basic_calls))

# Scenario 2: a catalog miss must not poison the next local tool decision.
reasonix_messages = [
    {
        "role": "user",
        "content": "Use use_capability to search the local catalog for ssh-manager, then summarize only that result.",
    }
]
_, reasonix_todo, reasonix_calls = complete(reasonix_messages, required_tool="use_capability")
reasonix_messages.append(
    {
        "role": "user",
        "content": f"Now use read_file or bash to read {fixture}; do not ask me to run the command.",
    }
)
reasonix_text, _, followup_calls = complete(reasonix_messages, reasonix_todo)
if marker not in reasonix_text or not ({"read_file", "bash"} & set(followup_calls)):
    raise AssertionError("catalog miss prevented the subsequent local tool call")
print("PASS reasonix_catalog_miss", ",".join(reasonix_calls + followup_calls))

# Scenario 3: verify the streaming tool-call envelope and finish reason.
stream_body = {
    "model": model,
    "stream": True,
    "messages": [{"role": "user", "content": f"Use read_file to read {fixture}."}],
    "tools": tools,
}
chunks = post_stream(stream_body)
stream_calls = [
    call
    for chunk in chunks
    for choice in chunk.get("choices", [])
    for call in choice.get("delta", {}).get("tool_calls", [])
]
finish_reasons = [
    choice.get("finish_reason")
    for chunk in chunks
    for choice in chunk.get("choices", [])
    if choice.get("finish_reason")
]
if len(stream_calls) != 1 or stream_calls[0]["function"]["name"] != "read_file":
    raise AssertionError("stream did not contain one read_file call")
if finish_reasons != ["tool_calls"]:
    raise AssertionError("stream finish reason was not tool_calls")
print("PASS openai_stream read_file")
PY
