import asyncio
from deploy_agent.factory import build_default_checkpointer


async def main():
    saver = build_default_checkpointer()
    tup = await saver.aget_tuple({"configurable": {"thread_id": "test-thread-003"}})
    if tup is None:
        print("NO CHECKPOINT")
        return
    cp = tup.checkpoint
    ch = cp.get("channel_values") or {}
    msgs = ch.get("messages") or ch.get("messages+") or []
    print("latest checkpoint ts:", cp.get("ts"))
    print("messages in checkpoint:", len(msgs))
    for m in msgs:
        role = type(m).__name__
        content = getattr(m, "content", "")
        text = content if isinstance(content, str) else str(content)[:60]
        print(f"  [{role}] {text[:70]}")
    await saver.conn.close()


asyncio.run(main())