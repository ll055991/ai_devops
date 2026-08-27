import httpx
import json
import uuid

url = 'http://127.0.0.1:8000/api/agent/chat'

# ========== 关键：指定线程ID，相同id复用记忆 ==========
# 方案1：固定ID，多次运行都用同一个会话，保留历史记忆
thread_id = "test-thread-003"
# 方案2：新建唯一会话（每次全新上下文）
# thread_id = str(uuid.uuid4())

with httpx.Client(timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10)) as c:
    # 请求体带上 thread_id
    payload = {
        "thread_id": thread_id,
        "message": "暗号是什么"
    }
    with c.stream('POST', url, json=payload) as r:
        ev = ''
        for line in r.iter_lines():
            if not line:
                continue
            if line.startswith('event: '):
                ev = line[7:].strip()
            elif line.startswith('data: ') and ev == 'tool_call_end':
                d = json.loads(line[6:])
                print(f'\n[{ev}] {d.get("name","")} result={json.dumps(d.get("result",""), ensure_ascii=False)[:200]}')
            elif line.startswith('data: ') and ev == 'message_delta':
                d = json.loads(line[6:])
                if d.get('text'):
                    print(d['text'], end='', flush=True)