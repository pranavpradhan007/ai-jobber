import os
os.environ["JOB_AGENT_DB"] = "job_agent.db"
from src.db.connection import get_connection
from src.gmail.digest import build_digest

conn = get_connection("job_agent.db")
result = build_digest(
    conn,
    recipient="pranavpradhan00721@gmail.com",
    dry_run=True,
)
print(f"\nDigest ID: {result}")

# Show the full email
from src.gmail.digest import _render_digest, _digest_id
from src.db.applications import get_applications_by_state
from datetime import datetime, timezone

pending = get_applications_by_state(conn, "WAITING_FOR_USER_APPROVAL")
now = datetime.now(timezone.utc)
did = _digest_id(now.isoformat())
subject, body = _render_digest(conn, pending, did, now)

print(f"\nSUBJECT: {subject}")
print("\nBODY:")
print(body)
conn.close()
