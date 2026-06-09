"""
Scrape a LinkedIn Easy Apply job and insert it into the DB as an approved candidate
ready for fill_only / pause_before_submit testing.
"""
import os, sys, time
os.environ['JOB_AGENT_DB'] = 'job_agent.db'

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

EASY_APPLY_IDS = [
    '4421639774',  # Founding Machine Learning Engineer
    '4425525087',  # Artificial Intelligence Engineer
    '4425909496',  # AI/ML Developer
    '4424264423',  # Artificial Intelligence Engineer
    '4413409947',  # Data Scientist
    '4424748133',  # Data Scientist
    '4414263901',  # AI Implementation Engineer
]

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp('http://localhost:9222', timeout=20000)
    ctx = b.contexts[0]
    li_page = None
    for p in ctx.pages:
        if 'linkedin.com' in p.url:
            li_page = p
            break
    if not li_page:
        li_page = ctx.new_page()

    inserted_app_id = None

    for job_id in EASY_APPLY_IDS:
        url = f'https://www.linkedin.com/jobs/view/{job_id}'
        print(f'\nScraping {url} ...')
        try:
            li_page.goto(url, wait_until='domcontentloaded', timeout=25000)
        except Exception as e:
            print(f'  goto error: {e}')
            continue
        time.sleep(3)

        # Title
        try:
            title = li_page.locator('h1').first.inner_text(timeout=5000).strip()
        except:
            title = 'Unknown'

        # Company
        try:
            company_sels = [
                '.jobs-unified-top-card__company-name',
                'a.app-aware-link[data-tracking-control-name*="company"]',
                '.job-details-jobs-unified-top-card__company-name',
            ]
            company = 'Unknown'
            for sel in company_sels:
                el = li_page.locator(sel).first
                if el.count() > 0:
                    company = el.inner_text(timeout=2000).strip()
                    break
        except:
            company = 'Unknown'

        # JD text
        try:
            jd_sels = [
                '.jobs-description__content',
                '#job-details',
                '.jobs-box__html-content',
                '[class*="description__text"]',
            ]
            jd = ''
            for sel in jd_sels:
                el = li_page.locator(sel).first
                if el.count() > 0:
                    jd = el.inner_text(timeout=3000).strip()
                    if len(jd) > 100:
                        break
        except:
            jd = ''

        # Easy Apply button
        try:
            ea_btn = li_page.locator(
                'button.jobs-apply-button, button[aria-label*="Easy Apply"], button:has-text("Easy Apply")'
            ).first
            has_ea = ea_btn.count() > 0
        except:
            has_ea = False

        print(f'  Title:    {title[:60]}')
        print(f'  Company:  {company[:40]}')
        print(f'  JD len:   {len(jd)}')
        print(f'  EasyApply:{has_ea}')

        if not has_ea:
            print('  Skipping — no Easy Apply')
            continue
        if len(jd) < 50:
            print('  Skipping — JD too short')
            continue

        # Insert into DB
        from src.db.connection import get_connection
        from src.db.jobs import create_job, get_job_by_url
        from src.db.applications import create_application, update_application, transition
        from src.storage.folders import create_application_folder
        import pathlib
        import src.storage.folders as fm
        fm.APPLICATIONS_BASE = str(pathlib.Path('applications').resolve())
        pathlib.Path('applications').mkdir(exist_ok=True)

        conn = get_connection('job_agent.db')

        existing = get_job_by_url(conn, url)
        if existing:
            print(f'  Job already in DB: JOB-{existing.id}')
            app_row = conn.execute(
                "SELECT id, state FROM applications WHERE job_id=?", (existing.id,)
            ).fetchone()
            if app_row:
                app_id = app_row['id']
                update_application(conn, app_id, approved_by_user=1)
                transition(conn, app_id, 'WAITING_FOR_USER_APPROVAL',
                           reason='fast-track fill test')
                conn.commit()
                print(f'  Updated APP-{app_id} → WAITING_FOR_USER_APPROVAL + approved=1')
                inserted_app_id = app_id
        else:
            job = create_job(conn,
                url=url,
                company=company,
                title=title,
                location='New York, NY',
                remote=False,
                raw_jd=jd,
                clean_jd=jd,
                platform='linkedin',
                source='linkedin_browser',
            )
            print(f'  Created JOB-{job.id}')

            app_id = create_application(conn, job.id)
            create_application_folder(conn, app_id)
            update_application(conn, app_id, score=75.0, approved_by_user=1)
            transition(conn, app_id, 'WAITING_FOR_USER_APPROVAL',
                       reason='fast-track fill test')
            conn.commit()
            print(f'  Created APP-{app_id} approved+WAITING (score=75.0)')
            inserted_app_id = app_id

        conn.close()
        break

    b.close()

if inserted_app_id:
    print(f'\nReady: APP-{inserted_app_id} is approved and waiting.')
    print('Run: python run_fill_only.py')
else:
    print('\nNo job inserted. Try a different job ID.')
