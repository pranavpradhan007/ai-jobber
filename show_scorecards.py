import os, json, pathlib
os.environ["JOB_AGENT_DB"] = "job_agent.db"
from src.db.connection import get_connection

conn = get_connection("job_agent.db")

for app_id in [2, 3, 4]:
    row = conn.execute(
        "SELECT a.*, j.company, j.title FROM applications a "
        "JOIN jobs j ON j.id=a.job_id WHERE a.id=?", (app_id,)
    ).fetchone()

    print(f"\n{'='*60}")
    print(f"APP-{app_id}: {row['company']} — {row['title']}")
    print(f"  State        : {row['state']}")
    print(f"  Score        : {row['score']:.1f}/100")
    print(f"  Tier         : {row['submit_tier']}")
    print(f"  Verifier     : {'PASSED' if row['verifier_passed'] else 'FAILED'}")
    print(f"  Folder       : {row['folder_path']}")

    sc_path = row["scorecard_path"]
    if sc_path and pathlib.Path(sc_path).exists():
        sc = json.loads(pathlib.Path(sc_path).read_text())
        print(f"  Scorecard breakdown:")
        for d in sc["dimensions"]:
            bar = "#" * int(d["raw"] / 10)
            print(f"    {d['name']:22s} {d['raw']:5.0f}  {bar:10s}  {d['reason']}")

    folder = row["folder_path"]
    if folder:
        fp = pathlib.Path(folder)
        files = list(fp.glob("*")) if fp.exists() else []
        print(f"  Artifacts ({len(files)}):")
        for f in sorted(files):
            size = f.stat().st_size
            print(f"    {f.name:35s} {size:>8,} bytes")

conn.close()
