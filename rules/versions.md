# Rule: Version Boundaries

## V1 — Local laptop (this repo)

**In scope:**
- Local SQLite database
- Local file system for artifacts
- Overnight batch runner (cron / Task Scheduler)
- Gmail API (send/receive) via OAuth from local credentials
- Playwright browser automation on the local machine
- Job discovery from provided URLs or CSV imports

**Out of scope (hard block):**
- Cloud deployment of any component
- Outreach or cold-email campaigns
- LinkedIn scraping or automation
- Distributed queue or worker fleet
- SaaS integrations beyond Gmail

## V2 — Outreach (future)

Will add outreach / cold-email campaigns. Not a single line of V2 code belongs
in V1. If a design choice would "make V2 easier", defer it.

## V3 — Cloud (future)

Will containerise and deploy to cloud. Not a single line of V3 infrastructure
(Docker, Terraform, cloud SDKs) belongs in V1.

## Enforcement

When wearing any role, if a proposed change is V2 or V3 scope, **reject it and
note the version boundary violation**. The EM confirms V1-only scope at the
start of every sprint ceremony.
