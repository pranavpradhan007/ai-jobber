import os
os.environ["JOB_AGENT_DB"] = "job_agent.db"
from src.db.connection import get_connection
conn = get_connection("job_agent.db")

print(f"{'APP':<5} {'COMPANY':<32} {'STATE':<37} {'SCORE':<7} {'TIER':<12} NOTES")
print("-" * 100)
for r in conn.execute(
    "SELECT a.id, j.company, a.state, a.score, a.submit_tier, "
    "a.approved_by_user, a.edit_instruction "
    "FROM applications a JOIN jobs j ON j.id=a.job_id ORDER BY a.id"
):
    notes = []
    if r["approved_by_user"]:
        notes.append("APPROVED - ready to submit")
    if r["edit_instruction"]:
        notes.append(f'EDIT: "{r["edit_instruction"][:40]}"')
    print(
        f"APP-{r['id']:<2} "
        f"{(r['company'] or '')[:30]:<32} "
        f"{r['state'][:36]:<37} "
        f"{str(round(r['score'],1) if r['score'] else 'n/a'):<7} "
        f"{(r['submit_tier'] or ''):<12} "
        f"{', '.join(notes)}"
    )
conn.close()
