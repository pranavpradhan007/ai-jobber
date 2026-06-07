"""
One-time fix: update states for apps whose forms were submitted but whose
DB state transition was blocked by the verifier_passed=0 bug (now fixed).

Run:  python scripts/fix_submitted_states.py
"""
import sqlite3, datetime, sys, os

db_path = os.path.join(os.path.dirname(__file__), "..", "job_agent.db")
conn = sqlite3.connect(db_path)
now = datetime.datetime.utcnow().isoformat()

updates = [
    # APP-43 (Obin AI → Gem): submitted to jobs.gem.com, blocked by verifier_passed=0
    (43, "WAITING_FOR_USER_APPROVAL", "MONITORING",
     "PORTAL:custom:https://jobs.gem.com/obin-ai/am9icG9zdDrtea8l6d-xDWE4wSFhQzV6?rcid=linkedin"),
    # APP-45 (Parafin → Ashby): submitted to jobs.ashbyhq.com, blocked by verifier_passed=0
    (45, "WAITING_FOR_USER_APPROVAL", "MONITORING",
     "PORTAL:ashby:https://jobs.ashbyhq.com/parafin/cb15e569-764f-4304-a2cd-41a11fe9e008/application?src=linkedin"),
]

for app_id, from_state, to_state, receipt in updates:
    row = conn.execute("SELECT id, state FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        print(f"APP-{app_id}: not found — skipping")
        continue
    if row[1] != from_state:
        print(f"APP-{app_id}: state is {row[1]}, expected {from_state} — skipping (already updated?)")
        continue
    conn.execute(
        "UPDATE applications SET state=?, submitted_at=?, receipt=? WHERE id=?",
        (to_state, now, receipt, app_id),
    )
    print(f"APP-{app_id}: {from_state} -> {to_state}  (submitted_at={now})")

# APP-46 (Robinhood): AI_TRAP_DETECTED was a false positive (aria-hidden+tabindex=-1 is Greenhouse's
# file widget, not a honeypot). The trap detector is now fixed. Reset to WAITING_FOR_USER_APPROVAL.
row46 = conn.execute("SELECT state FROM applications WHERE id=46").fetchone()
if row46 and row46[0] == "AI_TRAP_DETECTED":
    conn.execute(
        "UPDATE applications SET state='WAITING_FOR_USER_APPROVAL' WHERE id=46",
    )
    print("APP-46: AI_TRAP_DETECTED -> WAITING_FOR_USER_APPROVAL  (false positive, trap detector fixed)")
else:
    print(f"APP-46: state is {row46[0] if row46 else 'not found'} — skipping")

conn.commit()
conn.close()

print("\nFinal states:")
conn2 = sqlite3.connect(db_path)
for row in conn2.execute(
    "SELECT id, state, submitted_at FROM applications WHERE id IN (41,42,43,44,45,46,49)"
).fetchall():
    print(f"  APP-{row[0]}: {row[1]}  submitted_at={row[2]}")
conn2.close()
