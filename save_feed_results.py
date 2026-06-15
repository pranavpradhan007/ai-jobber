"""Save feed discovery results and import them."""
from src.discovery.feeds import write_feed_results, import_cached_feeds
from src.db.connection import get_connection

# We Work Remotely jobs extracted
wwr_jobs = [
    {
        "title": "Product Owner - Machine Learning",
        "company": "Mitek Systems",
        "location": "Anywhere in the World",
        "url": "https://weworkremotely.com/remote-jobs/mitek-systems-product-owner-machine-learning",
        "snippet": "Own and manage the backlog for ML-driven biometric and document verification capabilities."
    },
    {
        "title": "Full Stack Engineer (AI-Forward)",
        "company": "Chief Rebel",
        "location": "Anywhere in the World",
        "url": "https://weworkremotely.com/remote-jobs/chief-rebel-full-stack-engineer-ai-forward",
        "snippet": "Build and extend a production-grade platform running Next.js, Go, Supabase, and an async job pipeline."
    },
    {
        "title": "System Engineer/DevOps - Senior",
        "company": "Softswiss",
        "location": "Anywhere in the World",
        "url": "https://weworkremotely.com/remote-jobs/softswiss-system-engineer-devops-senior",
        "snippet": "Design, automate, and maintain scalable infrastructure and deployment pipelines."
    },
    {
        "title": "Staff Product Security Engineer - Customer Platform",
        "company": "Valon Tech",
        "location": "Anywhere in the World",
        "url": "https://weworkremotely.com/remote-jobs/valon-tech-staff-product-security-engineer-customer-platform",
        "snippet": "Define and evolve product security architecture and strategy for Valon's multi-tenant SaaS platform."
    },
    {
        "title": "Senior Product Designer, AI Platform",
        "company": "Vanta",
        "location": "Anywhere in the World",
        "url": "https://weworkremotely.com/remote-jobs/vanta-senior-product-designer-ai-platform",
        "snippet": "Enhance and expand the Agentic experiences to meet evolving platform needs."
    }
]

print("Saving We Work Remotely feed results...")
write_feed_results("feed_wwr_20260605_165529", "weworkremotely", wwr_jobs)

# Import the cached feeds
conn = get_connection()
summary = import_cached_feeds(conn)
print(f"\n=== FEED IMPORT SUMMARY ===")
print(f"Total added: {summary.get('added', 0)}")
print(f"By source: {summary.get('sources', {})}")

conn.close()
