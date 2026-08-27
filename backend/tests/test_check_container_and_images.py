import httpx
import json

url = 'http://127.0.0.1:8001/api/agent/chat'

with httpx.Client(timeout=60) as c:
    with c.stream('POST', url, json={'message': '你有清理容器的工具吗？'}) as r:
        ev = ''
        for line in r.iter_lines():
            if line.startswith('event: '):
                ev = line[7:].strip()
            elif line.startswith('data: ') and ev == 'tool_call_end':
                d = json.loads(line[6:])
                print(f'[{ev}] {d.get("name","")} result={json.dumps(d.get("result",""), ensure_ascii=False)[:200]}')
            elif line.startswith('data: ') and ev == 'message_delta':
                d = json.loads(line[6:])
                if d.get('text'):
                    print(d['text'], end='', flush=True)