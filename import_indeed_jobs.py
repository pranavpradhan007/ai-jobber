"""Import the Indeed jobs collected via MCP API into the database."""
import sqlite3

from src.db.connection import get_connection
from src.discovery.indeed import import_jobs

# Job data collected from Indeed MCP API calls
jobs_data = [
    {
        "url": "https://to.indeed.com/aapqx2xftm47",
        "company": "DataAnnotation",
        "title": "Application Engineer - AI Trainer",
        "location": "Pearland, TX",
        "description": "Join the DataAnnotation team and contribute to developing cutting-edge AI systems.",
    },
    {
        "url": "https://to.indeed.com/aaks8yn8wdds",
        "company": "Indeed",
        "title": "Distinguished Engineer, AI",
        "location": "Remote",
        "description": "As Distinguished Engineer, AI at Indeed, you will be instrumental in revolutionizing hiring through artificial intelligence.",
    },
    {
        "url": "https://to.indeed.com/aazv6rywtxhp",
        "company": "The Hershey Company",
        "title": "Staff Engineer OT Digital Systems",
        "location": "Hershey, PA",
        "description": "OT Data Engineer – Unified Namespace is responsible for designing and implementing scalable industrial data solutions.",
    },
    {
        "url": "https://to.indeed.com/aay7v8qcqllv",
        "company": "David Joseph & Company",
        "title": "AI Engineer — RapidCanvas",
        "location": "Austin, TX",
        "description": "Design, train, and deploy machine learning models and LLM-powered systems for RapidCanvas platform.",
    },
    {
        "url": "https://to.indeed.com/aap8v69896bx",
        "company": "RepoBird",
        "title": "Junior AI Engineer",
        "location": "Remote",
        "description": "Join our innovative team as a Junior AI Engineer and be at the forefront of developing AI solutions.",
    },
    {
        "url": "https://to.indeed.com/aaygt27krrw2",
        "company": "Northramp",
        "title": "AI Engineer (Mid)",
        "location": "Washington, DC",
        "description": "Design and build intelligent, agentic applications on Google Cloud Platform.",
    },
    {
        "url": "https://to.indeed.com/aaf7qn7tw98g",
        "company": "SNO",
        "title": "Artificial Intelligence Engineer (Cloud & AI Solutions)",
        "location": "Remote",
        "description": "Deploy, configure, and maintain AI-powered tools and solutions within cloud environments.",
    },
    {
        "url": "https://to.indeed.com/aaqpqf2mdvmn",
        "company": "Flexential",
        "title": "Data & AI Solution Engineer",
        "location": "Wildwood, MO",
        "description": "Design, build, and operate enterprise-grade data, integration, and AI-enabling solutions.",
    },
    {
        "url": "https://to.indeed.com/aajb9skmh7hb",
        "company": "BV Teck",
        "title": "AI/ML Engineer with Drone Imagery",
        "location": "Remote",
        "description": "Design, develop, and deploy AI/ML models for drone imagery analysis and aerial image processing.",
    },
    {
        "url": "https://to.indeed.com/aanwmncpggvk",
        "company": "Roboflow",
        "title": "Product Engineer",
        "location": "Remote",
        "description": "Build computer vision models and ship products that make the world programmable with AI.",
    }
]

def main():
    conn = get_connection()

    print("Importing Indeed jobs...")
    summary = import_jobs(conn, jobs_data, source="indeed")

    print("\n=== IMPORT SUMMARY ===")
    print(f"Added:              {summary.get('added', 0)}")
    print(f"Skipped (duplicate): {summary.get('skipped_duplicate', 0)}")
    print(f"Skipped (no JD):     {summary.get('skipped_no_jd', 0)}")
    print(f"New app IDs:         {summary.get('new_app_ids', [])}")

    conn.close()

if __name__ == "__main__":
    main()
