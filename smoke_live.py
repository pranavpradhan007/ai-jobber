"""Live smoke test script — run once, shows full system state."""
import os, json, sys
os.environ["JOB_AGENT_DB"] = "job_agent.db"
from src.db.connection import get_connection

conn = get_connection("job_agent.db")

print("=" * 60)
print("DB HEALTH")
print("=" * 60)
for tbl in ["jobs", "applications", "source_bank", "digests", "state_transitions"]:
    n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl:25s} {n:>5} rows")

print()
print("SOURCE BANK BY TYPE")
for row in conn.execute("SELECT item_type, COUNT(*) n FROM source_bank GROUP BY item_type ORDER BY item_type"):
    print(f"  {row['item_type']:15s} {row['n']:>4}")

print()
print("APPLICATIONS BY STATE")
rows = conn.execute("SELECT state, COUNT(*) n FROM applications GROUP BY state ORDER BY state").fetchall()
if rows:
    for row in rows:
        print(f"  {row['state']:40s} {row['n']:>3}")
else:
    print("  (none yet)")
conn.close()
