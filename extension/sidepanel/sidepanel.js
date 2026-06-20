// Side panel controller

let _currentJobCtx   = null;
let _currentFillResult = null;
let _activityLog     = [];

// ─── Utilities ────────────────────────────────────────────────────────────────

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .trim();
}

function ts() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function logActivity(icon, text, color) {
  _activityLog.push({ ts: ts(), icon, text, color: color || '#5f6368' });
  const el = document.getElementById('activityLog');
  if (!el) return;
  el.style.display = '';
  el.innerHTML = _activityLog.slice(-8).map(e =>
    `<div style="color:${e.color}"><span style="opacity:0.5">${e.ts}</span> ${e.icon} ${e.text}</div>`
  ).join('');
  el.scrollTop = el.scrollHeight;
}

// ─── Verification banner ──────────────────────────────────────────────────────
// States: filling | verifying | ok | warn | error

function setVerifyBanner(state, text, sub) {
  const banner = document.getElementById('verifyBanner');
  const icon   = document.getElementById('verifyIcon');
  const textEl = document.getElementById('verifyText');
  const subEl  = document.getElementById('verifySub');
  const dot    = document.getElementById('headerDot');
  if (!banner) return;

  banner.style.display = '';
  banner.className = 'verify-banner state-' + state;

  const ICONS = { filling: '⏳', verifying: '🔍', ok: '✅', warn: '⚠️', error: '✗' };
  icon.textContent   = ICONS[state] || '⏳';
  textEl.textContent = text || '';
  subEl.textContent  = sub  || '';

  // Header dot
  dot.className = 'header-dot';
  if (state === 'filling' || state === 'verifying') dot.classList.add('yellow');
  else if (state === 'error' || state === 'warn') dot.classList.add('red');
}

// ─── Real-time Claude progress (from service worker broadcasts) ───────────────

function updateClaudeProgress(msg) {
  switch (msg.stage) {
    case 'drafter_start':
      setVerifyBanner('verifying', `Filling ${msg.fieldsCount} fields with Claude…`, 'Drafter running');
      logActivity('✏', `Drafter started — ${msg.fieldsCount} fields`, '#e65100');
      break;
    case 'drafter_done':
      setVerifyBanner('verifying', 'Reviewing answer quality…', 'Drafter complete');
      logActivity('✓', `Drafter done — ${msg.answersCount} answers`, '#1a73e8');
      if (msg.preview) {
        Object.entries(msg.preview).slice(0, 3).forEach(([k, v]) => {
          logActivity(' ›', `${k}: "${String(v).slice(0, 55)}${String(v).length > 55 ? '…' : ''}"`, '#80868b');
        });
      }
      break;
    case 'reviewer_start':
      setVerifyBanner('verifying', 'Reviewing for quality & hallucinations…', 'Reviewer running');
      logActivity('🔍', 'Reviewer checking quality', '#7b1fa2');
      break;
    case 'reviewer_done':
      setVerifyBanner('verifying', 'Verifying all filled fields…', `Reviewed by ${msg.reviewedBy}`);
      logActivity('✓', `Reviewer done — ${msg.reviewedBy}`, '#0f9d58');
      break;
    case 'verify_start':
      setVerifyBanner('verifying', `Verifying ${msg.fieldsCount} fields with Claude…`, 'Checking for gaps');
      logActivity('🔍', `Verification pass started — ${msg.fieldsCount} fields`, '#1a73e8');
      break;
    case 'verify_done':
      logActivity('✓', `Verification done — ${msg.correctionCount} corrections, ${msg.emptyCount} empty required`, '#0f9d58');
      break;
    case 'error':
      setVerifyBanner('error', 'Claude error', msg.error || '');
      logActivity('✗', msg.error || 'Unknown error', '#ea4335');
      break;
  }
}

// ─── Field list rendering ─────────────────────────────────────────────────────

function renderFields(fillResult, jobCtx, claudeStatus) {
  _currentFillResult = fillResult;
  _currentJobCtx = jobCtx;

  // Job strip
  if (jobCtx) {
    document.getElementById('jobTitle').textContent   = jobCtx.title   || 'Job Application';
    document.getElementById('jobCompany').textContent = jobCtx.company || '';
    document.getElementById('jobStrip').style.display = '';
  }

  // Verification banner
  _renderBannerFromStatus(claudeStatus, fillResult);

  // Empty required section
  const emptyReq = claudeStatus?.emptyRequired || claudeStatus?.verifyResult?.emptyRequired || [];
  const emptySection = document.getElementById('emptyRequiredSection');
  const emptyList    = document.getElementById('emptyRequiredList');
  const emptyCount   = document.getElementById('emptyRequiredCount');
  if (emptyReq.length > 0) {
    emptySection.style.display = '';
    emptyCount.textContent = emptyReq.length;
    emptyList.innerHTML = emptyReq.map(label =>
      `<div class="empty-field-item">× ${label}</div>`
    ).join('');
  } else {
    emptySection.style.display = 'none';
  }

  // Fields list
  const listEl = document.getElementById('fieldsList');
  const emptyEl = document.getElementById('fieldsEmpty');
  const fields = fillResult.fields || [];

  if (fields.length === 0) {
    emptyEl.style.display = '';
    listEl.innerHTML = '';
    return;
  }
  emptyEl.style.display = 'none';

  // Group fields by source
  const groups = { verified: [], corrected: [], claude: [], profile: [], memory: [], skip: [] };
  for (const f of fields) {
    const src = f.source || 'profile';
    if (groups[src]) groups[src].push(f);
    else groups.profile.push(f);
  }

  const GROUP_LABELS = {
    verified:  '✓ Verified by Claude',
    corrected: '✎ Corrected by Claude',
    claude:    '✨ Filled by Claude',
    profile:   '👤 Filled from Profile',
    memory:    '🧠 Filled from Memory',
    skip:      '— Skipped',
  };
  const BADGE_CLASS = {
    verified:  'badge-verified',
    corrected: 'badge-corrected',
    claude:    'badge-claude',
    profile:   'badge-profile',
    memory:    'badge-memory',
    skip:      'badge-skip',
  };
  const ROW_CLASS = {
    verified:  'verified',
    corrected: 'corrected',
    claude:    'from-claude',
  };

  listEl.innerHTML = '';

  for (const [src, srcFields] of Object.entries(groups)) {
    if (!srcFields.length) continue;
    const groupLabel = document.createElement('div');
    groupLabel.className = 'fields-group-label';
    groupLabel.textContent = GROUP_LABELS[src] || src;
    listEl.appendChild(groupLabel);

    for (const f of srcFields) {
      const row = document.createElement('div');
      row.className = 'field-row' + (ROW_CLASS[src] ? ' ' + ROW_CLASS[src] : '');

      const meta = document.createElement('div');
      meta.className = 'field-meta';

      const lbl = document.createElement('div');
      lbl.className = 'field-lbl';
      lbl.textContent = f.label || 'Field';

      const val = document.createElement('div');
      const v = String(f.value || '—');
      val.className = 'field-val' + (v.length > 60 ? ' multiline' : '');
      val.textContent = v;

      meta.appendChild(lbl);
      meta.appendChild(val);

      const badge = document.createElement('span');
      badge.className = 'field-badge ' + (BADGE_CLASS[src] || 'badge-profile');
      badge.textContent = src;

      row.appendChild(meta);
      row.appendChild(badge);

      // Click to edit
      row.addEventListener('click', () => {
        if (row.classList.contains('editing')) return;
        row.classList.add('editing');
        const ta = document.createElement('textarea');
        ta.className = 'field-edit-input';
        ta.value = String(f.value || '');
        ta.rows = Math.min(4, Math.max(2, Math.ceil(ta.value.length / 50)));
        row.appendChild(ta);
        ta.focus();
        ta.addEventListener('blur', async () => {
          const newVal = ta.value.trim();
          row.classList.remove('editing');
          ta.remove();
          if (newVal && newVal !== f.value) {
            f.value = newVal;
            val.textContent = newVal;
            badge.textContent = 'edited';
            badge.className = 'field-badge badge-corrected';
            await chrome.runtime.sendMessage({ type: 'SAVE_MEMORY_EDIT', key: normalizeLabel(f.label || ''), label: f.label, answer: newVal });
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (tab) chrome.tabs.sendMessage(tab.id, { type: 'FILL_FIELD', label: f.label, value: newVal });
          }
        });
      });

      listEl.appendChild(row);
    }
  }
}

function _renderBannerFromStatus(claudeStatus, fillResult) {
  if (!claudeStatus) {
    setVerifyBanner('filling', 'Starting autofill…', '');
    return;
  }

  const src   = claudeStatus.source;
  const stage = claudeStatus.stage;
  const filled = fillResult?.filled || 0;
  const claude = fillResult?.claude  || 0;

  if (stage === 'calling' || src === 'calling') {
    setVerifyBanner('filling', `Manual autofill done — verifying ${claudeStatus.fieldsCount} fields with Claude…`, `${filled} filled from profile so far`);
    return;
  }
  if (stage === 'verifying' || src === 'verifying') {
    setVerifyBanner('verifying', 'Verifying all filled fields with Claude…', `${filled} fields filled`);
    return;
  }
  if (src === 'rate_limited') {
    setVerifyBanner('warn', 'Claude rate limit hit — please verify before applying', claudeStatus.error || '');
    logActivity('⚠', 'Rate limited — manual verification required', '#e65100');
    return;
  }
  if (src === 'proxy_down') {
    setVerifyBanner('error', 'Proxy not running — verify manually before applying', 'Run start_proxy.bat');
    return;
  }
  if (src === 'none_needed') {
    setVerifyBanner('ok', `All fields filled from profile (${filled} fields)`, 'Review before applying');
    return;
  }
  if (src === 'verified' || claudeStatus.ok) {
    const corrCount  = claudeStatus.correctionCount || 0;
    const emptyCount = (claudeStatus.emptyRequired || []).length;
    if (emptyCount > 0) {
      setVerifyBanner('warn',
        `${emptyCount} required field(s) still empty — fill manually`,
        `${filled} filled · ${claude} via Claude · ${corrCount} corrections`
      );
    } else {
      setVerifyBanner('ok',
        `Verified ✓ — ${filled} fields confirmed`,
        `${claude} via Claude · ${corrCount} correction(s) applied`
      );
      document.getElementById('activityLog').style.display = 'none';
    }
    return;
  }
  if (!claudeStatus.ok && claudeStatus.error) {
    setVerifyBanner('warn', 'Manual autofill complete — please verify before applying', claudeStatus.error.slice(0, 80));
    return;
  }
  // Default
  setVerifyBanner('filling', `Filling… (${filled} done so far)`, '');
}

// ─── Auto Apply ───────────────────────────────────────────────────────────────

let _autoApplyRunning = false;
let _autoApplyWatchdog = null;

function _resetAutoApplyWatchdog() {
  if (_autoApplyWatchdog) clearTimeout(_autoApplyWatchdog);
  _autoApplyWatchdog = setTimeout(() => {
    if (_autoApplyRunning) {
      _setAutoApplyRunning(false);
      document.getElementById('progressText').textContent = 'Timed out — no progress for 90s. Try again.';
    }
  }, 90000);
}

function _setAutoApplyRunning(running) {
  _autoApplyRunning = running;
  const btnStart = document.getElementById('btnAutoApply');
  const btnStop  = document.getElementById('btnStop');
  if (running) {
    btnStart.disabled = true;
    btnStart.textContent = '⚡ Auto Applying…';
    btnStop.style.display = 'block';
    _resetAutoApplyWatchdog();
  } else {
    btnStart.disabled = false;
    btnStart.textContent = '⚡ Auto Apply';
    btnStop.style.display = 'none';
    if (_autoApplyWatchdog) { clearTimeout(_autoApplyWatchdog); _autoApplyWatchdog = null; }
  }
}

document.getElementById('btnAutoApply').addEventListener('click', async () => {
  if (_autoApplyRunning) return;
  _setAutoApplyRunning(true);
  document.getElementById('progressText').textContent = 'Starting…';
  setVerifyBanner('filling', 'Starting Auto Apply…', '');
  _activityLog = [];

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: 'AUTOAPPLY_START' });
    } catch(e) {
      _setAutoApplyRunning(false);
      document.getElementById('progressText').textContent = 'Page not ready — try again in a moment.';
    }
  }
});

document.getElementById('btnStop').addEventListener('click', async () => {
  document.getElementById('progressText').textContent = 'Stopping…';
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) await chrome.tabs.sendMessage(tab.id, { type: 'AUTOAPPLY_STOP' });
});

// ─── Navigation buttons ───────────────────────────────────────────────────────

document.getElementById('btnNext').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) await chrome.tabs.sendMessage(tab.id, { type: 'NEXT_PAGE' });
});

document.getElementById('btnPrev').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) await chrome.tabs.sendMessage(tab.id, { type: 'GO_BACK' });
});

document.getElementById('btnSubmit').addEventListener('click', async () => {
  if (!confirm('Submit this application?')) return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) await chrome.tabs.sendMessage(tab.id, { type: 'SUBMIT_FORM', jobCtx: _currentJobCtx });
});

// ─── Message listener ─────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'FILL_COMPLETE') {
    if (msg.claudeStatus?.stage === 'calling' || msg.claudeStatus?.source === 'none_needed') {
      _activityLog = [];
    }
    renderFields(msg.fillResult, msg.jobCtx, msg.claudeStatus);
    if (msg.jobCtx?.jd_text) refreshKeywords(msg.jobCtx.jd_text);
  }
  if (msg.type === 'CLAUDE_PROGRESS') {
    updateClaudeProgress(msg);
  }
  if (msg.type === 'AUTOAPPLY_PROGRESS') {
    document.getElementById('progressText').textContent = msg.text || '';
    if (_autoApplyRunning) _resetAutoApplyWatchdog();
  }
  if (msg.type === 'AUTOAPPLY_DONE') {
    _setAutoApplyRunning(false);
    document.getElementById('progressText').textContent = msg.text || 'Done!';
  }
});

// ─── Tab switching ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.tab-panel').forEach(p => {
      p.classList.toggle('active', p.id === 'tab' + tab.charAt(0).toUpperCase() + tab.slice(1));
    });
    if (tab === 'history') renderHistory();
    if (tab === 'keywords' && _currentJobCtx) refreshKeywords(_currentJobCtx.jd_text || '');
  });
});

// ─── ATS keywords ─────────────────────────────────────────────────────────────

async function refreshKeywords(jdText) {
  if (!jdText) return;
  const result = await chrome.runtime.sendMessage({ type: 'CHECK_ATS', jdText });
  if (!result?.keywords?.length) return;

  const present = result.keywords.filter(k => k.present);
  const missing = result.keywords.filter(k => !k.present);

  document.getElementById('kwEmpty').style.display = 'none';

  const pSec = document.getElementById('kwPresentSection');
  const mSec = document.getElementById('kwMissingSection');
  pSec.style.display = present.length ? '' : 'none';
  mSec.style.display = missing.length ? '' : 'none';

  document.getElementById('kwPresentChips').innerHTML = present.map(k =>
    `<span class="kw-chip present">✓ ${k.keyword}</span>`
  ).join('');

  document.getElementById('kwMissingChips').innerHTML = missing.map(k =>
    `<span class="kw-chip missing">✗ ${k.keyword}</span>`
  ).join('');
}

// ─── History ──────────────────────────────────────────────────────────────────

async function renderHistory() {
  const { applications = [] } = await chrome.storage.local.get('applications');
  const listEl  = document.getElementById('historyList');
  const emptyEl = document.getElementById('histEmpty');

  if (!applications.length) { emptyEl.style.display = ''; listEl.innerHTML = ''; return; }
  emptyEl.style.display = 'none';

  listEl.innerHTML = [...applications].reverse().map(a => {
    const d = new Date(a.applied_at);
    const dateStr = isNaN(d) ? a.applied_at : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    return `<div class="history-row">
      <div class="h-co">${a.company || '—'}</div>
      <div class="h-title">${a.title || '—'}</div>
      <div class="h-date">${dateStr} · ${a.status || 'logged'}</div>
    </div>`;
  }).join('');
}

// ─── Cover letter ─────────────────────────────────────────────────────────────

document.getElementById('btnGenCover').addEventListener('click', async () => {
  if (!_currentJobCtx) {
    document.getElementById('coverStatus').textContent = 'Autofill a job first.';
    return;
  }
  const btn = document.getElementById('btnGenCover');
  btn.disabled = true;
  btn.textContent = 'Generating…';
  document.getElementById('coverStatus').textContent = 'Calling Claude…';

  const result = await chrome.runtime.sendMessage({ type: 'GENERATE_COVER_LETTER', jobCtx: _currentJobCtx });
  btn.disabled = false;
  btn.textContent = 'Generate';

  if (result?.text) {
    document.getElementById('coverLetterText').value = result.text;
    document.getElementById('coverStatus').textContent = 'Generated! Edit then copy or fill.';
  } else {
    document.getElementById('coverStatus').textContent = 'Failed — proxy may be offline.';
  }
});

document.getElementById('btnCopyCover').addEventListener('click', () => {
  const text = document.getElementById('coverLetterText').value;
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    document.getElementById('coverStatus').textContent = 'Copied!';
    setTimeout(() => { document.getElementById('coverStatus').textContent = ''; }, 2000);
  });
});

document.getElementById('btnFillCover').addEventListener('click', async () => {
  const text = document.getElementById('coverLetterText').value;
  if (!text) return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    chrome.tabs.sendMessage(tab.id, { type: 'FILL_FIELD', label: 'cover letter', value: text });
    document.getElementById('coverStatus').textContent = 'Filled!';
  }
});

// ─── Restore last fill on panel open ─────────────────────────────────────────

chrome.storage.local.get(['lastFillResult', 'lastJobCtx', 'lastClaudeStatus']).then(({ lastFillResult, lastJobCtx, lastClaudeStatus }) => {
  if (lastFillResult) renderFields(lastFillResult, lastJobCtx, lastClaudeStatus);
  if (lastJobCtx) { _currentJobCtx = lastJobCtx; refreshKeywords(lastJobCtx.jd_text || ''); }
});
