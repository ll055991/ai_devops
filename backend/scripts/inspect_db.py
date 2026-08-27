import sqlite3

conn = sqlite3.connect("checkpoints/checkpoints.db")
cur = conn.cursor()
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)
for t in tables:
    try:
        cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
        if "thread_id" in cols:
            rows = cur.execute(f"SELECT thread_id, COUNT(*) FROM {t} GROUP BY thread_id").fetchall()
            print(t, "->", rows)
    except Exception as e:
        print(t, "ERR", e)
conn.close()