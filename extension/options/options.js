// Options page — full profile editor + Claude Code status

// ─── Navigation ───────────────────────────────────────────────────────────────

const PAGES = {
  claude:  'pageClaude',
  profile: 'pageProfile',
  work:    'pageWork',
  edu:     'pageEdu',
  resume:  'pageResume',
  memory:  'pageMemory',
  tracker: 'pageTracker',
};

document.querySelectorAll('.nav-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    Object.values(PAGES).forEach(id => document.getElementById(id).style.display = 'none');
    const page = tab.dataset.page;
    document.getElementById(PAGES[page]).style.display = '';
    if (page === 'memory')  loadMemory();
    if (page === 'tracker') loadTracker();
  });
});

if (location.hash === '#tracker') document.querySelector('[data-page=tracker]').click();

// ─── Proxy / Claude Code status ───────────────────────────────────────────────

async function checkProxy() {
  const dot = document.getElementById('proxyDot');
  const status = document.getElementById('proxyStatus');
  try {
    const r = await fetch('http://localhost:3747/v1/messages', {
      method: 'OPTIONS',
      signal: AbortSignal.timeout(1200),
    });
    dot.className = 'dot dot-green';
    status.textContent = 'Proxy running — Claude Code connected';
    status.style.color = '#0f9d58';
  } catch(e) {
    dot.className = 'dot dot-red';
    status.textContent = 'Proxy not running — start extension/start_proxy.bat';
    status.style.color = '#ea4335';
  }
}

async function checkRateLimit() {
  const dot = document.getElementById('rateDot');
  const status = document.getElementById('rateLimitStatus');
  const { claudeRateLimited, claudeRateLimitedAt } = await chrome.storage.local.get(
    ['claudeRateLimited', 'claudeRateLimitedAt']
  );
  const limited = claudeRateLimited && (Date.now() - (claudeRateLimitedAt || 0) < 3600_000);
  if (limited) {
    const mins = Math.ceil((3600_000 - (Date.now() - claudeRateLimitedAt)) / 60000);
    dot.className = 'dot dot-orange';
    status.textContent = `Rate-limited (~${mins} min remaining) — fallback mode active`;
    status.style.color = '#f57c00';
  } else {
    dot.className = 'dot dot-green';
    status.textContent = 'Not rate-limited — Claude active';
    status.style.color = '#0f9d58';
  }
}

document.getElementById('btnTestProxy').addEventListener('click', async () => {
  document.getElementById('proxyStatus').textContent = 'Testing...';
  await checkProxy();
});

document.getElementById('btnClearRateLimit').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'CLEAR_RATE_LIMIT' });
  checkRateLimit();
});

document.getElementById('btnReloadProfile').addEventListener('click', async () => {
  const btn = document.getElementById('btnReloadProfile');
  btn.textContent = 'Reloading...';
  try {
    const url = chrome.runtime.getURL('data/profile.json');
    const resp = await fetch(url);
    const freshProfile = await resp.json();
    const { profile: existing = {} } = await chrome.storage.local.get('profile');
    await chrome.storage.local.set({ profile: { ...existing, ...freshProfile } });
    await loadSettings();
    btn.textContent = '✓ Reloaded';
    setTimeout(() => btn.textContent = '↺ Reload Profile Defaults', 2000);
  } catch(e) {
    btn.textContent = 'Error — check console';
  }
});

// ─── Load all settings into form ─────────────────────────────────────────────

async function loadSettings() {
  const { profile: p = {} } = await chrome.storage.local.get('profile');

  // Personal
  setVal('pFirstName',     p.first_name || '');
  setVal('pLastName',      p.last_name || '');
  setVal('pPreferredName', p.preferred_name || '');
  setVal('pEmail',         p.email || '');
  setVal('pPhone',         p.phone || '');
  setVal('pPhoneCode',     p.phone_country_code || '');

  // Address
  setVal('pAddress',  p.address || '');
  setVal('pCity',     p.city || '');
  setVal('pState',    p.state || '');
  setVal('pZip',      p.zip_code || '');
  setVal('pCountry',  p.country || '');

  // Online
  setVal('pLinkedin',  p.linkedin_url || '');
  setVal('pGithub',    p.github_url || '');
  setVal('pPortfolio', p.portfolio_url || '');
  setVal('pOtherUrl',  p.other_url || '');

  // Professional
  setVal('pTitle',         p.current_title || '');
  setVal('pYears',         p.years_experience || '');
  setVal('pSalary',        p.salary_expectation || '');
  setVal('pSalaryDisplay', p.salary_display || '');
  setVal('pNoticePeriod',  p.notice_period || '2 weeks');
  setVal('pStartDate',     p.start_date || '2 weeks');
  setVal('pSkills',        p.skills || '');
  setVal('pBio',           p.bio || '');
  setSelectVal('pRemote',    p.remote_preference || 'Open to remote or hybrid');
  setSelectVal('pRelocate',  p.willing_to_relocate || 'Open to discuss');

  // Work auth
  setSelectVal('pWorkAuth',    p.work_authorization || '');
  setVal('pVisaStatus',        p.visa_status || '');
  setSelectVal('pSponsorship', p.requires_sponsorship || 'No');
  setSelectVal('pUsCitizen',   p.us_citizen_or_pr || 'No');

  // EEO
  setSelectVal('pGender',     p.eeo_gender || '');
  setSelectVal('pEthnicity',  p.eeo_ethnicity || '');
  setSelectVal('pVeteran',    p.eeo_veteran || '');
  setSelectVal('pDisability', p.eeo_disability || '');

  // Cover letter / statements
  setVal('pCoverLetter', p.cover_letter_default || '');
  setVal('pWhyAI',       p.why_ai || '');
  setVal('pStrength',    p.strength || '');

  // Education
  const edu = p.education_history || [];
  const e0 = edu[0] || {};
  const e1 = edu[1] || {};
  setVal('eSchool1',    e0.school || p.education_school || '');
  setVal('eDegree1',    e0.degree || p.education_degree?.split(' in ')[0] || '');
  setVal('eField1',     e0.field  || p.education_degree?.split(' in ')[1] || '');
  setVal('eGpa1',       e0.gpa   || p.education_gpa || '');
  setVal('eStart1',     e0.start_year || '');
  setVal('eEnd1',       e0.end_year   || '');
  setVal('eLoc1',       e0.location   || '');
  setVal('eGradYear',   p.graduation_year || '');
  setVal('eCoursework1', e0.coursework || '');
  setVal('eSchool2',    e1.school || '');
  setVal('eDegree2',    e1.degree || '');
  setVal('eField2',     e1.field  || '');
  setVal('eGpa2',       e1.gpa    || '');
  setVal('eStart2',     e1.start_year || '');
  setVal('eEnd2',       e1.end_year   || '');
  setVal('eLoc2',       e1.location   || '');

  // Work history
  renderWorkEntries(p.work_history || []);

  // Resume
  const { resume_b64 } = await chrome.storage.local.get('resume_b64');
  if (resume_b64) document.getElementById('resumeFileName').textContent = 'Resume loaded ✓';
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function setSelectVal(id, val) {
  const el = document.getElementById(id);
  if (!el || !val) return;
  for (const opt of el.options) {
    if (opt.value === val || opt.text === val) { el.value = opt.value; return; }
  }
  // Not found — add it
  const opt = document.createElement('option');
  opt.value = val; opt.text = val;
  el.appendChild(opt);
  el.value = val;
}

// ─── Work history ─────────────────────────────────────────────────────────────

let _workEntries = [];

function renderWorkEntries(entries) {
  _workEntries = entries.length ? entries : [emptyWork()];
  const container = document.getElementById('workEntries');
  container.innerHTML = '';
  _workEntries.forEach((w, i) => container.appendChild(buildWorkCard(w, i)));
}

function emptyWork() {
  return { title: '', company: '', location: '', start_year: '', end_year: '', current: false, description: '' };
}

function buildWorkCard(w, i) {
  const div = document.createElement('div');
  div.className = 'work-entry';
  div.dataset.idx = i;
  div.innerHTML = `
    <div class="work-entry-header" style="display:flex;justify-content:space-between;align-items:center">
      <span>Role ${i + 1}</span>
      <button class="btn btn-danger work-del" data-idx="${i}" style="font-size:11px;padding:3px 10px">Remove</button>
    </div>
    <div class="grid-2">
      <div class="form-group"><label>Job Title</label><input type="text" class="w-title" value="${esc(w.title)}"></div>
      <div class="form-group"><label>Company</label><input type="text" class="w-company" value="${esc(w.company)}"></div>
      <div class="form-group"><label>Location</label><input type="text" class="w-location" value="${esc(w.location)}"></div>
      <div class="form-group">
        <label>Currently Here</label>
        <select class="w-current">
          <option value="false" ${!w.current ? 'selected' : ''}>No</option>
          <option value="true"  ${ w.current ? 'selected' : ''}>Yes (current)</option>
        </select>
      </div>
      <div class="form-group"><label>Start Year</label><input type="text" class="w-start" value="${esc(w.start_year || w.start_month ? (w.start_month ? w.start_month + '/' : '') + (w.start_year || '') : '')}"></div>
      <div class="form-group"><label>End Year (or blank if current)</label><input type="text" class="w-end" value="${esc(w.end_year || '')}"></div>
      <div class="form-group full">
        <label>Role Description / Key Achievements</label>
        <textarea class="w-desc" rows="3">${esc(w.description)}</textarea>
      </div>
    </div>
  `;
  div.querySelector('.work-del').addEventListener('click', () => {
    _workEntries.splice(i, 1);
    renderWorkEntries(_workEntries);
  });
  return div;
}

function esc(s) { return (s || '').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }

document.getElementById('btnAddWork').addEventListener('click', () => {
  _workEntries.push(emptyWork());
  renderWorkEntries(_workEntries);
});

function collectWorkEntries() {
  const cards = document.querySelectorAll('.work-entry');
  return Array.from(cards).map(card => ({
    title:       card.querySelector('.w-title').value.trim(),
    company:     card.querySelector('.w-company').value.trim(),
    location:    card.querySelector('.w-location').value.trim(),
    current:     card.querySelector('.w-current').value === 'true',
    start_year:  card.querySelector('.w-start').value.trim(),
    end_year:    card.querySelector('.w-end').value.trim(),
    description: card.querySelector('.w-desc').value.trim(),
  })).filter(w => w.title || w.company);
}

// ─── Save ─────────────────────────────────────────────────────────────────────

document.getElementById('btnSave').addEventListener('click', async () => {
  const { profile: existing = {} } = await chrome.storage.local.get('profile');

  const firstName = document.getElementById('pFirstName').value.trim();
  const lastName  = document.getElementById('pLastName').value.trim();

  const profile = {
    ...existing,
    first_name:           firstName,
    last_name:            lastName,
    full_name:            [firstName, lastName].filter(Boolean).join(' '),
    preferred_name:       document.getElementById('pPreferredName').value.trim() || firstName,
    email:                document.getElementById('pEmail').value.trim(),
    phone:                document.getElementById('pPhone').value.trim(),
    phone_country_code:   document.getElementById('pPhoneCode').value.trim(),
    address:              document.getElementById('pAddress').value.trim(),
    city:                 document.getElementById('pCity').value.trim(),
    state:                document.getElementById('pState').value.trim(),
    zip_code:             document.getElementById('pZip').value.trim(),
    country:              document.getElementById('pCountry').value.trim(),
    linkedin_url:         document.getElementById('pLinkedin').value.trim(),
    github_url:           document.getElementById('pGithub').value.trim(),
    portfolio_url:        document.getElementById('pPortfolio').value.trim(),
    other_url:            document.getElementById('pOtherUrl').value.trim(),
    current_title:        document.getElementById('pTitle').value.trim(),
    years_experience:     document.getElementById('pYears').value.trim(),
    salary_expectation:   document.getElementById('pSalary').value.trim(),
    salary_display:       document.getElementById('pSalaryDisplay').value.trim(),
    notice_period:        document.getElementById('pNoticePeriod').value.trim(),
    start_date:           document.getElementById('pStartDate').value.trim(),
    remote_preference:    document.getElementById('pRemote').value,
    willing_to_relocate:  document.getElementById('pRelocate').value,
    skills:               document.getElementById('pSkills').value.trim(),
    bio:                  document.getElementById('pBio').value.trim(),
    work_authorization:   document.getElementById('pWorkAuth').value,
    visa_status:          document.getElementById('pVisaStatus').value.trim(),
    requires_sponsorship: document.getElementById('pSponsorship').value,
    us_citizen_or_pr:     document.getElementById('pUsCitizen').value,
    eeo_gender:           document.getElementById('pGender').value,
    eeo_ethnicity:        document.getElementById('pEthnicity').value,
    eeo_veteran:          document.getElementById('pVeteran').value,
    eeo_disability:       document.getElementById('pDisability').value,
    cover_letter_default: document.getElementById('pCoverLetter').value.trim(),
    why_ai:               document.getElementById('pWhyAI').value.trim(),
    strength:             document.getElementById('pStrength').value.trim(),
    graduation_year:      document.getElementById('eGradYear').value.trim(),
    education_school:     document.getElementById('eSchool1').value.trim(),
    education_degree:     [document.getElementById('eDegree1').value.trim(), document.getElementById('eField1').value.trim()].filter(Boolean).join(' in '),
    education_gpa:        document.getElementById('eGpa1').value.trim(),
    education_history: [
      {
        school:     document.getElementById('eSchool1').value.trim(),
        degree:     document.getElementById('eDegree1').value.trim(),
        field:      document.getElementById('eField1').value.trim(),
        gpa:        document.getElementById('eGpa1').value.trim(),
        start_year: document.getElementById('eStart1').value.trim(),
        end_year:   document.getElementById('eEnd1').value.trim(),
        location:   document.getElementById('eLoc1').value.trim(),
        coursework: document.getElementById('eCoursework1').value.trim(),
      },
      {
        school:     document.getElementById('eSchool2').value.trim(),
        degree:     document.getElementById('eDegree2').value.trim(),
        field:      document.getElementById('eField2').value.trim(),
        gpa:        document.getElementById('eGpa2').value.trim(),
        start_year: document.getElementById('eStart2').value.trim(),
        end_year:   document.getElementById('eEnd2').value.trim(),
        location:   document.getElementById('eLoc2').value.trim(),
      },
    ].filter(e => e.school || e.degree),
    work_history: collectWorkEntries(),
  };

  await chrome.storage.local.set({ profile });

  const toast = document.getElementById('toast');
  toast.style.display = 'block';
  setTimeout(() => toast.style.display = 'none', 2000);
});

// ─── Resume upload ────────────────────────────────────────────────────────────

document.getElementById('resumeDropZone').addEventListener('click', () => {
  document.getElementById('resumeFile').click();
});

['dragover', 'dragenter'].forEach(evt => {
  document.getElementById('resumeDropZone').addEventListener(evt, e => {
    e.preventDefault();
    e.currentTarget.style.borderColor = '#1a73e8';
  });
});

document.getElementById('resumeDropZone').addEventListener('drop', e => {
  e.preventDefault();
  e.currentTarget.style.borderColor = '';
  const file = e.dataTransfer.files[0];
  if (file) loadResumeFile(file);
});

document.getElementById('resumeFile').addEventListener('change', e => {
  const file = e.target.files[0];
  if (file) loadResumeFile(file);
});

function loadResumeFile(file) {
  if (!file.type.includes('pdf')) { alert('Please upload a PDF file'); return; }
  const reader = new FileReader();
  reader.onload = async e => {
    const b64 = e.target.result.split(',')[1];
    await chrome.storage.local.set({ resume_b64: b64 });
    document.getElementById('resumeFileName').textContent = `✓ ${file.name} (${Math.round(file.size / 1024)} KB)`;
  };
  reader.readAsDataURL(file);
}

// ─── Memory viewer ────────────────────────────────────────────────────────────

async function loadMemory() {
  const { memory = {} } = await chrome.storage.local.get('memory');
  const container = document.getElementById('memoryList');
  container.innerHTML = '';
  const entries = Object.entries(memory);
  if (!entries.length) {
    container.innerHTML = '<p style="color:#80868b;font-size:13px">No learned answers yet. Start applying!</p>';
    return;
  }
  for (const [key, data] of entries) {
    const row = document.createElement('div');
    row.className = 'memory-row';
    row.innerHTML = `
      <div class="memory-key">${key}</div>
      <div class="memory-val">
        <div>${data.answer || ''}</div>
        <div class="memory-meta">${data.source || ''} · ${data.seen || 0}× · ${data.last_seen || ''}</div>
      </div>
      <button data-key="${key}" class="btn btn-danger mem-del" style="font-size:11px;padding:3px 10px;flex-shrink:0">✕</button>
    `;
    container.appendChild(row);
  }
  container.querySelectorAll('.mem-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      const { memory: m = {} } = await chrome.storage.local.get('memory');
      delete m[btn.dataset.key];
      await chrome.storage.local.set({ memory: m });
      btn.closest('.memory-row').remove();
    });
  });
}

document.getElementById('btnClearMemory').addEventListener('click', async () => {
  if (!confirm('Clear all learned answers?')) return;
  await chrome.storage.local.set({ memory: {} });
  loadMemory();
});

// ─── Applications tracker ─────────────────────────────────────────────────────

async function loadTracker() {
  const { applications = [] } = await chrome.storage.local.get('applications');
  const container = document.getElementById('trackerContent');
  if (!applications.length) {
    container.innerHTML = '<p style="text-align:center;color:#80868b;padding:30px">No applications yet.</p>';
    return;
  }
  let html = `<table class="app-table"><thead><tr>
    <th>#</th><th>Company</th><th>Role</th><th>Date</th><th>Status</th><th>Link</th>
  </tr></thead><tbody>`;
  [...applications].reverse().forEach((app, i) => {
    const date = app.applied_at ? new Date(app.applied_at).toLocaleDateString() : '—';
    html += `<tr>
      <td>${applications.length - i}</td>
      <td>${app.company || '—'}</td>
      <td>${app.title || '—'}</td>
      <td>${date}</td>
      <td><span class="status-chip">${app.status || 'applied'}</span></td>
      <td><a href="${app.url || '#'}" target="_blank" style="color:#1a73e8;font-size:12px">Open →</a></td>
    </tr>`;
  });
  html += '</tbody></table>';
  container.innerHTML = html;
}

// ─── Init ─────────────────────────────────────────────────────────────────────

loadSettings();
checkProxy();
checkRateLimit();
