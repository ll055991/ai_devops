"""SSE 流式接口冒烟测试。

用法：
1. 先启动后端：
   uv run uvicorn deploy_agent.api:app --host 127.0.0.1 --port 8000
2. 另开终端跑本脚本：
   uv run python scripts/smoke_test.py

打印所有收到的 SSE 事件，手动观察事件顺序是否符合需求文档第六章。
"""

from __future__ import annotations

import json
import sys

import httpx

# 后端地址
BASE_URL = "http://127.0.0.1:8001"

# 测试消息（让 LLM 读 SKILL.md 后按流程执行，分支从系统提示词读取）
TEST_MESSAGE = "请先读 /skills/deployment/SKILL.md，然后按标准部署流程执行部署"


def main() -> int:
    """发起流式 POST，打印所有 SSE 事件。"""
    url = f"{BASE_URL}/api/agent/chat"
    payload = {"message": TEST_MESSAGE}

    print(f"[smoke_test] POST {url}")
    print(f"[smoke_test] message={TEST_MESSAGE!r}")
    print("-" * 60)

    event_count = 0
    try:
        # 流式读取响应
        with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)) as client:
            with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    print(f"[smoke_test] ERROR: status={response.status_code}")
                    print(response.text)
                    return 1

                event_name = ""
                data_lines: list[str] = []

                for line in response.iter_lines():
                    if not line:
                        # 空行表示一条 SSE 事件结束
                        if event_name:
                            event_count += 1
                            data_str = "\n".join(data_lines)
                            try:
                                data = json.loads(data_str) if data_str else {}
                            except json.JSONDecodeError:
                                data = {"_raw": data_str}

                            # 简洁打印
                            preview = _preview_event(event_name, data)
                            print(f"#{event_count:03d} [{event_name}] {preview}")
                            event_name = ""
                            data_lines = []
                        continue

                    # 解析 SSE 行
                    if line.startswith("event: "):
                        event_name = line[len("event: ") :].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[len("data: ") :])

    except httpx.ConnectError:
        print("[smoke_test] ERROR: 无法连接后端，请先启动 uvicorn")
        return 1
    except KeyboardInterrupt:
        print("\n[smoke_test] 用户中断")
        return 0

    print("-" * 60)
    print(f"[smoke_test] 共收到 {event_count} 个事件")
    return 0


def _preview_event(event_name: str, data: dict) -> str:
    """生成事件预览文本（简洁打印）。"""
    if event_name == "agent_state":
        return f"status={data.get('status')}"
    if event_name == "message_delta":
        text = data.get("text", "")
        return f"text={text[:60]!r}{'...' if len(text) > 60 else ''}"
    if event_name == "tool_call_start":
        return f"name={data.get('name')} call_id={data.get('call_id','')[:12]}"
    if event_name == "tool_call_end":
        name = data.get("name", "")
        result = data.get("result", "")
        result_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        return f"name={name} result={result_str[:80]!r}{'...' if len(result_str) > 80 else ''}"
    if event_name == "task_status":
        return f"status={data.get('status')} tool={data.get('tool')}"
    if event_name == "log":
        return f"tool={data.get('tool')} text={data.get('text','')[:60]!r}"
    if event_name == "approval_required":
        actions = data.get("action_requests", [])
        names = [a.get("name", "") for a in actions if isinstance(a, dict)]
        return f"tools={names}"
    if event_name == "stream_complete":
        result = data.get("final_result", "")
        return f"final_result={result[:80]!r}{'...' if len(result) > 80 else ''}"
    if event_name == "error":
        return f"message={data.get('message','')}"
    # 兜底
    return json.dumps(data, ensure_ascii=False)[:100]


if __name__ == "__main__":
    sys.exit(main())
