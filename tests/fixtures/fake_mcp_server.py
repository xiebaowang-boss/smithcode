"""测试用假 MCP 服务器：单行 JSON-RPC over stdio。

通过环境变量 FAKE_MCP_MODE 选择行为：
- default：提供 echo / slow / fail / big / structured 五个工具
- no-tools：tools/list 返回空
- bad-json：启动时先往 stdout 打一行非 JSON（模拟不守规范的 server）
- exit：立即退出（握手前进程消失）
- hang：不响应 initialize（握手超时）
- list-changed：initialized 后立即推送 tools/list_changed 通知
"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MCP_MODE", "default")

# MCP 规范要求 stdio 走 UTF-8；Windows 默认代码页（GBK）会让严格解码的客户端报错。
try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except (AttributeError, ValueError):
    pass

TOOLS = [
    {
        "name": "echo",
        "description": "回显输入",
        "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}},
    },
    {
        "name": "slow",
        "description": "慢工具",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fail",
        "description": "总是失败",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "big",
        "description": "超大输出",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "structured",
        "description": "结构化结果",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

if MODE == "crash-tool":
    TOOLS = TOOLS + [{
        "name": "crash",
        "description": "直接退出进程",
        "inputSchema": {"type": "object", "properties": {}},
    }]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(request_id, payload):
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if MODE == "hang":
            time.sleep(60)
            return
        result(request_id, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": MODE == "list-changed"}},
            "serverInfo": {"name": "fake", "version": "0.1"},
        })
        return
    if method == "notifications/initialized":
        if MODE == "list-changed":
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})
        return
    if method == "tools/list":
        result(request_id, {"tools": [] if MODE == "no-tools" else TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            result(request_id, {"content": [{"type": "text", "text": f"echo: {arguments.get('msg', '')}"}]})
        elif name == "slow":
            time.sleep(2)
            result(request_id, {"content": [{"type": "text", "text": "slow done"}]})
        elif name == "fail":
            result(request_id, {"content": [{"type": "text", "text": "boom"}], "isError": True})
        elif name == "big":
            result(request_id, {"content": [{"type": "text", "text": "x" * 30000}]})
        elif name == "structured":
            result(request_id, {
                "content": [{"type": "text", "text": "{\"ok\": true}"}],
                "structuredContent": {"ok": True, "items": [1, 2]},
            })
        elif name == "crash":
            sys.stdout.flush()
            os._exit(1)
        else:
            error(request_id, -32602, f"unknown tool: {name}")
        return
    if method == "notifications/cancelled":
        return
    if request_id is not None:
        error(request_id, -32601, "Method not found")


def main():
    if MODE == "exit":
        sys.exit(1)
    if MODE == "bad-json":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(message)


if __name__ == "__main__":
    main()
