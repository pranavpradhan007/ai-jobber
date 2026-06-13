"""
LinkedIn job discovery via headless Playwright browser.

Uses the JobAgent Chrome profile (saved session) to avoid re-login.
Falls back to headless Chromium with credentials if CDP not available.

Flow:
  1. Connect to Chrome CDP or launch headless
  2. Navigate to linkedin.com/jobs/search for each (role, location) pair
  3. Scroll through result cards, extract job metadata
  4. Visit each job page to get the description + detect Easy Apply
  5. Return list of job dicts for import_jobs()

Anti-detection:
  - Uses real Chrome with saved cookies (CDP mode)
  - Human-like pauses between actions
  - Random scroll amounts
  - No aggressive parallel scraping
"""
from __future__ import annotations
import logging
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_CDP_URL = "http://localhost:9222"
_CDP_PORT_DEFAULT = 9222

# Role + location search matrix for LinkedIn
LINKEDIN_ROLES = [
    "Machine Learning Engineer",
    "AI Engineer",
    "LLM Engineer",
    "Applied Scientist",
    "Research Engineer deep learning",
    "MLOps Engineer",
    "Data Scientist machine learning",
    "Reinforcement Learning Engineer",
    "NLP Engineer",
]

LINKEDIN_LOCATIONS = [
    "United States",
    "Remote",
    "New York, NY",
    "San Francisco, CA",
    "Seattle, WA",
    "Boston, MA",
    "Austin, TX",
]

# Filters: posted in past month, full-time, experience level
_JOB_FILTER_PARAMS = {
    "f_TPR": "r2592000",   # past 30 days
    "f_JT":  "F",          # full-time
    "sortBy": "DD",        # most recent
}


@dataclass
class LinkedInJob:
    job_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    easy_apply: bool = False
    apply_url: str = ""
    posted_at: str = ""
    extra: dict = field(default_factory=dict)


def discover_jobs(
    role_queries: list[str] | None = None,
    locations: list[str] | None = None,
    max_per_search: int = 20,
    headless: bool = True,
    cdp_port: int = _CDP_PORT_DEFAULT,
) -> list[dict]:
    """
    Main entry point. Returns list of job dicts compatible with import_jobs().
    """
    role_queries = role_queries or LINKEDIN_ROLES
    locations = locations or LINKEDIN_LOCATIONS

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright not installed — run: pip install playwright && playwright install chromium")
        return []

    jobs: list[LinkedInJob] = []
    seen_ids: set[str] = set()

    with sync_playwright() as pw:
        browser, page = _connect_browser(pw, headless, cdp_port=cdp_port)
        try:
            if not _ensure_logged_in(page):
                logger.error("LinkedIn login required — open JobAgent Chrome, log into linkedin.com, then retry")
                return []

            for role in role_queries[:6]:  # cap at 6 roles per run to avoid rate limits
                for loc in locations[:4]:  # cap at 4 locations per role
                    try:
                        found = _search_jobs(page, role, loc, max_per_search, seen_ids)
                        jobs.extend(found)
                        logger.info("linkedin role=%r loc=%r found=%d", role, loc, len(found))
                        _human_pause(3.0, 6.0)
                    except Exception as exc:
                        logger.warning("linkedin search failed role=%r loc=%r: %s", role, loc, exc)

        finally:
            try:
                if browser:
                    # disconnect() releases our Playwright handle without closing Chrome
                    # pages (close() on a CDP connection would kill all open tabs).
                    browser.disconnect()
            except Exception:
                pass

    return [_to_job_dict(j) for j in jobs]


def _connect_browser(pw, headless: bool, cdp_port: int = _CDP_PORT_DEFAULT):
    """Try CDP first (saved session), fall back to fresh headless Chromium."""
    cdp_url = f"http://localhost:{cdp_port}"
    for attempt in range(3):
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=30000)
            contexts = browser.contexts
            if contexts and contexts[0].pages:
                page = contexts[0].pages[0]
            else:
                ctx = browser.new_context() if not contexts else contexts[0]
                page = ctx.new_page()
            logger.info("linkedin: connected via CDP (saved session, attempt %d)", attempt + 1)
            return browser, page
        except Exception as exc:
            logger.info("linkedin: CDP connect attempt %d failed (%s)", attempt + 1, exc)
            import time as _t; _t.sleep(2)

    logger.info("linkedin: CDP unavailable after 3 attempts, launching headless Chromium")

    browser = pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()
    return browser, page


def _ensure_logged_in(page) -> bool:
    """Navigate to linkedin.com and check for login state."""
    try:
        page.goto("https://www.linkedin.com/jobs/", timeout=20_000, wait_until="domcontentloaded")
        _human_pause(1.5, 3.0)
        url = page.url
        # If redirected to login page, not authenticated
        if "linkedin.com/login" in url or "linkedin.com/checkpoint" in url:
            logger.warning("linkedin: not logged in (redirected to %s)", url)
            return False
        # Check for sign-in button as fallback
        if page.locator("a[href*='/login']").count() > 0:
            text = page.content()
            if "Sign in" in text and "jobs" not in page.url:
                return False
        return True
    except Exception as exc:
        logger.warning("linkedin: login check failed: %s", exc)
        return False


def _search_jobs(
    page,
    role: str,
    location: str,
    max_results: int,
    seen_ids: set[str],
) -> list[LinkedInJob]:
    """Search LinkedIn jobs for (role, location), return extracted listings."""
    params = {
        "keywords": role,
        "location": location,
        **_JOB_FILTER_PARAMS,
    }
    search_url = "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)

    try:
        page.goto(search_url, timeout=25_000, wait_until="domcontentloaded")
        _human_pause(2.0, 4.0)
    except Exception as exc:
        logger.warning("linkedin: navigation failed for %r: %s", search_url, exc)
        return []

    # Scroll to load more cards
    _scroll_results(page, scrolls=4)

    # Extract card stubs from the sidebar
    jobs = _extract_job_cards(page, seen_ids)

    # Enrich by clicking each card in-sidebar (avoids page navigation, JD loads in right panel)
    enriched: list[LinkedInJob] = []
    cards = page.locator("[data-occludable-job-id]")
    for job in jobs[:max_results]:
        try:
            # Click the card to load detail panel (no page.goto needed)
            card = cards.filter(has=page.locator(f"[data-occludable-job-id='{job.job_id}']")).first
            if card.count() == 0:
                card = page.locator(f"[data-occludable-job-id='{job.job_id}']").first
            if card.count() > 0:
                card.click()
                _human_pause(1.5, 2.5)
            _enrich_job_from_panel(page, job)
            enriched.append(job)
            seen_ids.add(job.job_id)
        except Exception as exc:
            logger.warning("linkedin: enrich failed job_id=%s: %s", job.job_id, exc)
            enriched.append(job)  # keep partial data

    return enriched


def _scroll_results(page, scrolls: int = 4):
    """Scroll the results panel to load more listings."""
    for _ in range(scrolls):
        try:
            panel = page.locator(".jobs-search__results-list, ul.scaffold-layout__list-container").first
            if panel.count() > 0:
                page.evaluate("(el) => el.scrollBy(0, 600)", panel.element_handle())
            else:
                page.evaluate("window.scrollBy(0, 600)")
        except Exception:
            pass
        _human_pause(0.8, 1.5)


def _extract_job_cards(page, seen_ids: set[str]) -> list[LinkedInJob]:
    """Parse job cards from the search results sidebar."""
    jobs: list[LinkedInJob] = []

    # LinkedIn job card selectors (try in order; DOM changes over time)
    card_selectors = [
        "[data-occludable-job-id]",            # 2025+ authenticated feed (primary)
        "div[data-job-id]",                    # older authenticated feed
        "li.jobs-search__results-list > div",  # legacy authenticated feed
        "li.job-search-card",                  # public feed
        "ul.jobs-search__results-list li",
    ]

    cards = None
    for sel in card_selectors:
        locs = page.locator(sel)
        if locs.count() > 0:
            cards = locs
            break

    if cards is None:
        logger.warning("linkedin: no job cards found with known selectors")
        return []

    count = min(cards.count(), 30)
    for i in range(count):
        try:
            card = cards.nth(i)

            # Extract job ID from data attribute or URL
            job_id = (card.get_attribute("data-occludable-job-id")
                      or card.get_attribute("data-job-id")
                      or card.get_attribute("data-entity-urn") or "")
            if not job_id:
                link = card.locator("a[href*='/jobs/view/']").first
                if link.count() > 0:
                    href = link.get_attribute("href") or ""
                    m = re.search(r"/jobs/view/(\d+)", href)
                    if m:
                        job_id = m.group(1)
            if not job_id:
                continue
            # Normalize URN to numeric ID
            job_id = re.sub(r".*:", "", job_id)
            if job_id in seen_ids:
                continue

            title = _card_text(card, [
                ".job-card-list__title--link", ".job-card-list__title",  # 2025+
                "a[aria-label]", ".job-search-card__title",
                "h3.base-search-card__title", "span[aria-hidden='true']",
            ])
            company = _card_text(card, [
                ".job-card-container__company-name",                     # 2025+
                ".artdeco-entity-lockup__subtitle span",                 # 2025+
                ".job-card-container__primary-description",
                ".job-search-card__subtitle", "h4.base-search-card__subtitle",
                "a[data-tracking-control-name*='company']",
            ])
            location = _card_text(card, [
                ".job-card-container__metadata-item--workplace-type",    # 2025+
                ".job-card-container__metadata-item",
                ".job-search-card__location", "span.job-search-card__location",
                ".base-search-card__metadata span",
            ])

            link_el = card.locator("a[href*='/jobs/view/']").first
            href = link_el.get_attribute("href") if link_el.count() > 0 else ""
            if href and not href.startswith("http"):
                href = "https://www.linkedin.com" + href
            # Strip tracking params
            clean_url = re.sub(r"\?.*", "", href) if href else ""
            job_url = clean_url or f"https://www.linkedin.com/jobs/view/{job_id}"

            if not title or not company:
                continue

            jobs.append(LinkedInJob(
                job_id=job_id,
                title=title.strip(),
                company=company.strip(),
                location=location.strip() if location else "Remote",
                url=job_url,
            ))
        except Exception as exc:
            logger.debug("linkedin: card parse error idx=%d: %s", i, exc)

    return jobs


def _card_text(card, selectors: list[str]) -> str:
    """Try each selector, return first non-empty text found."""
    for sel in selectors:
        try:
            el = card.locator(sel).first
            if el.count() > 0:
                text = el.inner_text().strip()
                if text:
                    return text
        except Exception:
            pass
    return ""


def _enrich_job_from_panel(page, job: LinkedInJob):
    """Extract description and Easy Apply flag from the right-panel detail view.

    Called after clicking a search-result card — the detail panel loads inline
    without a page navigation, so all data is accessible immediately.
    """
    # Detect Easy Apply button in the detail panel.
    # Exclude buttons that live inside left-panel search-result cards
    # (those cards can show Easy Apply for OTHER jobs, causing false positives).
    try:
        ea_btn = page.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                return btns.find(b => {
                    const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
                    const txt = (b.innerText || '').trim().toLowerCase();
                    if (!(lbl.includes('easy apply') || txt === 'easy apply')) return false;
                    if (lbl.includes('filter')) return false;
                    // Exclude buttons inside left-panel job-card containers
                    if (b.closest('[data-occludable-job-id], .job-card-list__item, .jobs-search__results-list li')) return false;
                    return true;
                }) || null;
            })()
        """)
        job.easy_apply = ea_btn is not None
    except Exception:
        pass

    # External apply URL
    if not job.easy_apply:
        try:
            ext_href = page.evaluate("""
                (() => {
                    const sels = [
                        'a.jobs-apply-button[href]',
                        'a[data-tracking-control-name*="apply_link_click"]',
                        'a[aria-label*="Apply on"]',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const h = el.getAttribute('href') || '';
                            if (h.startsWith('http')) return h;
                        }
                    }
                    return '';
                })()
            """)
            if ext_href:
                job.apply_url = ext_href
        except Exception:
            pass

    # Description — check right panel selectors
    try:
        desc = page.evaluate("""
            (() => {
                const sels = [
                    '#job-details',
                    '.jobs-description__content',
                    '.jobs-box__html-content',
                    '[class*="description__text"]',
                    '.jobs-description-content__text',
                ];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.length > 100) {
                        return el.innerText.trim().slice(0, 6000);
                    }
                }
                return '';
            })()
        """)
        if desc:
            job.description = desc
    except Exception:
        pass


def _to_job_dict(job: LinkedInJob) -> dict:
    """Convert LinkedInJob to the dict format expected by import_jobs()."""
    return {
        "url": job.url,          # primary key used by import_jobs dedup
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "description": job.description,
        "raw_jd": job.description,
        "source": "linkedin_browser",
        "platform": "linkedin",
        "external_id": f"li_{job.job_id}",
        "easy_apply": job.easy_apply,
        "apply_method": "linkedin_easy_apply" if job.easy_apply else "external",
        "external_apply_url": job.apply_url,
    }


def _human_pause(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))
