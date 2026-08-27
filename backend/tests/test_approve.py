"""审批恢复测试脚本。

使用方法：
1. 先用 test_sse.py 发起部署请求，等待 approval_required 事件
2. 确认 thread_id 一致
3. 运行本脚本批准（approve）或拒绝（reject）审批
"""
import httpx
import json

url = "http://127.0.0.1:8001/api/agent/chat"

# ========== 必须与触发审批时的 thread_id 一致 ==========
thread_id = "test-thread-002"

# ========== 审批决策：approve=批准，reject=拒绝 ==========
decision_type = "approve"  # 改成 "reject" 可拒绝

payload = {
    "thread_id": thread_id,
    "decisions": [{"type": decision_type}],
}

print(f"发送审批决策: thread_id={thread_id}, decision={decision_type}")

with httpx.Client(timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10)) as c:
    with c.stream("POST", url, json=payload) as r:
        print(f"响应状态码: {r.status_code}")
        ev = ""
        for line in r.iter_lines():
            if not line:
                continue
            # SSE 注释行（心跳保活），跳过
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                ev = line[7:].strip()
            elif line.startswith("data: ") and ev == "tool_call_end":
                d = json.loads(line[6:])
                print(
                    f"\n[{ev}] {d.get('name', '')} "
                    f"result={json.dumps(d.get('result', ''), ensure_ascii=False)[:200]}"
                )
            elif line.startswith("data: ") and ev == "message_delta":
                d = json.loads(line[6:])
                if d.get("text"):
                    print(d["text"], end="", flush=True)
            elif line.startswith("data: ") and ev == "agent_state":
                d = json.loads(line[6:])
                print(f"\n[agent_state] {d.get('status', '')}")
            elif line.startswith("data: ") and ev == "stream_complete":
                print("\n[stream_complete]")
            elif line.startswith("data: ") and ev == "error":
                d = json.loads(line[6:])
                print(f"\n[error] {d.get('message', '')}")
