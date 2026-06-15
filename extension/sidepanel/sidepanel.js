// Side panel controller

let _currentJobCtx = null;
let _currentFillResult = null;
let _activityLog = [];

// ─── Activity log ─────────────────────────────────────────────────────────────

function ts() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function logActivity(icon, text, color) {
  _activityLog.push({ ts: ts(), icon, text, color: color || '#5f6368' });
  renderActivityLog();
}

function renderActivityLog() {
  const el = document.getElementById('claudeActivityLog');
  if (!el) return;
  el.innerHTML = _activityLog.map(e =>
    `<div style="color:${e.color}"><span style="opacity:0.5">${e.ts}</span> ${e.icon} ${e.text}</div>`
  ).join('');
  el.scrollTop = el.scrollHeight;
}

// ─── Claude progress (real-time from service worker) ─────────────────────────

function updateClaudeProgress(msg) {
  const bar  = document.getElementById('claudeStatusBar');
  const dot  = document.getElementById('claudeStatusDot');
  const text = document.getElementById('claudeStatusText');
  if (!bar) return;

  bar.style.display = '';

  const pulse = () => { dot.style.animation = 'pulse 1s infinite'; };
  const still = () => { dot.style.animation = ''; };

  switch (msg.stage) {
    case 'drafter_start': {
      dot.style.background = '#fbbc04'; pulse();
      text.textContent = `Claude: drafting (${msg.fieldsCount} fields)`;
      text.style.color = '#e65100';
      const fieldNames = (msg.fields || []).slice(0, 6).map(l => l.replace(/[^\w\s]/g,'').trim()).join(', ');
      logActivity('✏️', `Drafter started — ${msg.fieldsCount} fields: ${fieldNames}${msg.fieldsCount > 6 ? '…' : ''}`, '#e65100');
      break;
    }
    case 'drafter_done': {
      dot.style.background = '#fbbc04'; pulse();
      text.textContent = `Claude: reviewing (${msg.answersCount} answers)`;
      text.style.color = '#e65100';
      logActivity('✓', `Drafter done — ${msg.answersCount} answers generated`, '#1a73e8');
      // Show preview of first 3 answers
      if (msg.preview) {
        const entries = Object.entries(msg.preview).slice(0, 3);
        for (const [k, v] of entries) {
          const preview = String(v).slice(0, 60) + (String(v).length > 60 ? '…' : '');
          logActivity('  ›', `${k}: "${preview}"`, '#80868b');
        }
        if (Object.keys(msg.preview).length > 3) {
          logActivity('  ›', `…and ${Object.keys(msg.preview).length - 3} more`, '#80868b');
        }
      }
      break;
    }
    case 'reviewer_start': {
      dot.style.background = '#fbbc04'; pulse();
      text.textContent = 'Claude: reviewing quality…';
      text.style.color = '#e65100';
      logActivity('🔍', 'Reviewer checking for hallucinations & quality…', '#7b1fa2');
      break;
    }
    case 'reviewer_done': {
      dot.style.background = '#34a853'; still();
      text.textContent = `Claude: ${msg.answersCount} answers ready`;
      text.style.color = '#1b5e20';
      logActivity('✓', `Reviewer done — ${msg.reviewedBy} · ${msg.answersCount} final answers`, '#0f9d58');
      break;
    }
    case 'error': {
      dot.style.background = '#ea4335'; still();
      text.textContent = 'Claude: error';
      text.style.color = '#c62828';
      logActivity('✗', msg.error || 'Unknown error', '#ea4335');
      break;
    }
  }
}

// ─── Final status render (after fill complete) ────────────────────────────────

function renderClaudeStatus(claudeStatus) {
  const bar  = document.getElementById('claudeStatusBar');
  const dot  = document.getElementById('claudeStatusDot');
  const text = document.getElementById('claudeStatusText');
  if (!bar) return;

  bar.style.display = '';
  dot.style.animation = '';

  if (!claudeStatus) {
    dot.style.background = '#bdbdbd';
    text.textContent = 'Claude: not called';
    text.style.color = '#80868b';
    return;
  }

  if (claudeStatus.stage === 'calling') {
    dot.style.background = '#fbbc04';
    dot.style.animation = 'pulse 1s infinite';
    text.textContent = `Claude: calling… (${claudeStatus.fieldsCount} fields)`;
    text.style.color = '#e65100';
    if (!_activityLog.length) logActivity('⟳', `Waiting for Haiku via proxy…`, '#e65100');
    return;
  }

  const { ok, error, source, fieldsCount, reviewedBy } = claudeStatus;

  if (source === 'none_needed') {
    dot.style.background = '#34a853';
    text.textContent = 'Claude: not needed';
    text.style.color = '#2e7d32';
    logActivity('✓', 'All fields filled from profile/memory — no Claude needed', '#2e7d32');
    return;
  }
  if (source === 'rate_limited') {
    dot.style.background = '#fbbc04';
    text.textContent = 'Claude: rate limited';
    text.style.color = '#e65100';
    logActivity('⚠', error || 'Rate limited', '#e65100');
    return;
  }
  if (source === 'proxy_down') {
    dot.style.background = '#ea4335';
    text.textContent = 'Claude: proxy not running';
    text.style.color = '#c62828';
    logActivity('✗', error || 'Start start_proxy.bat', '#ea4335');
    return;
  }
  if (!ok) {
    dot.style.background = '#ea4335';
    text.textContent = `Claude: error at "${claudeStatus.stage || 'unknown'}"`;
    text.style.color = '#c62828';
    logActivity('✗', error || 'Unknown error', '#ea4335');
    return;
  }

  // Success
  dot.style.background = '#34a853';
  const answered = claudeStatus.answers ? Object.keys(claudeStatus.answers).length : (fieldsCount || 0);
  text.textContent = `Claude: filled ${answered} field${answered !== 1 ? 's' : ''}`;
  text.style.color = '#1b5e20';
  logActivity('✓', `Done — ${fieldsCount} sent · reviewed by ${reviewedBy}`, '#1b5e20');
}

function renderFields(fillResult, jobCtx, claudeStatus) {
  _currentFillResult = fillResult;
  _currentJobCtx = jobCtx;

  // Header
  if (jobCtx) {
    document.getElementById('jobTitle').textContent = jobCtx.title || 'Job Application';
    document.getElementById('jobCompany').textContent = jobCtx.company || '';
    document.getElementById('jobInfo').style.display = '';
  }

  // Stats bar — show Gmail-confirmed applied count
  document.getElementById('statsBar').style.display = '';
  refreshAppliedCount();

  // Claude status bar
  renderClaudeStatus(claudeStatus || null);

  // Source badge
  const badge = document.getElementById('sourceBadge');
  badge.textContent = fillResult.claude > 0 ? '● Claude' : '● Profile';
  badge.style.background = fillResult.claude > 0 ? 'rgba(123,31,162,0.3)' : 'rgba(255,255,255,0.25)';

  // Fields list
  const container = document.getElementById('tabFields');
  container.innerHTML = '';

  const fields = fillResult.fields || [];
  if (fields.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-icon">✓</div>All fields filled!</div>';
    return;
  }

  for (const f of fields) {
    const row = document.createElement('div');
    row.className = 'field-row';

    const labelDiv = document.createElement('div');
    labelDiv.className = 'field-label';
    labelDiv.innerHTML = `
      <span>${f.label || 'Field'}</span>
      <span class="field-source ${f.source === 'claude' ? 'claude' : ''}">${f.source || 'profile'}</span>
    `;

    const isLong = (f.value || '').length > 80;
    const input = isLong ? document.createElement('textarea') : document.createElement('input');
    input.className = 'field-value';
    input.value = f.value || '';
    if (isLong) {
      input.rows = Math.min(4, Math.ceil(f.value.length / 60));
    } else {
      input.type = 'text';
    }

    // Save edits to memory
    const fieldKey = normalizeLabel(f.label || '');
    input.addEventListener('blur', async () => {
      const newVal = input.value.trim();
      if (newVal && newVal !== f.value) {
        await chrome.runtime.sendMessage({
          type: 'SAVE_MEMORY_EDIT',
          key: fieldKey,
          label: f.label,
          answer: newVal,
        });
        f.value = newVal;
      }
    });

    row.appendChild(labelDiv);
    row.appendChild(input);
    container.appendChild(row);
  }
}

// ─── Applied count (Gmail-confirmed) ─────────────────────────────────────────

async function refreshAppliedCount() {
  const statEl  = document.getElementById('statApplied');
  const subEl   = document.getElementById('statAppliedSub');
  if (!statEl) return;

  // Show logged-submission count immediately as a placeholder
  const { applications = [] } = await chrome.storage.local.get('applications');
  const confirmed = applications.filter(a => a.status === 'confirmed').length;
  statEl.textContent = confirmed || applications.length;
  if (subEl) subEl.textContent = confirmed ? 'confirmed' : 'logged (checking Gmail…)';

  // Then verify via Gmail receipt count from proxy
  try {
    const resp = await fetch('http://localhost:3747/gmail/count-receipts', {
      signal: AbortSignal.timeout(60000),
    });
    if (resp.ok) {
      const data = await resp.json();
      const gmailCount = data.count ?? null;
      if (gmailCount !== null) {
        statEl.textContent = gmailCount;
        if (subEl) subEl.textContent = 'confirmed via Gmail';
        // Sync confirmed status back to storage
        await chrome.runtime.sendMessage({ type: 'SET_CONFIRMED_COUNT', count: gmailCount });
      }
    }
  } catch(e) {
    if (subEl) subEl.textContent = applications.length ? 'logged (proxy offline)' : '';
  }
}

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .trim();
}

// ─── Auto Apply ──────────────────────────────────────────────────────────────

let _autoApplyRunning = false;

document.getElementById('btnAutoApply').addEventListener('click', async () => {
  if (_autoApplyRunning) return;
  _autoApplyRunning = true;
  const btn = document.getElementById('btnAutoApply');
  const prog = document.getElementById('autoApplyProgress');
  btn.disabled = true;
  btn.textContent = '⚡ Auto Applying…';
  prog.textContent = 'Starting…';

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    await chrome.tabs.sendMessage(tab.id, { type: 'AUTOAPPLY_START' });
  }
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
  if (tab) {
    await chrome.tabs.sendMessage(tab.id, {
      type: 'SUBMIT_FORM',
      jobCtx: _currentJobCtx,
    });
  }
});

// ─── Listen for fill updates ──────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'FILL_COMPLETE') {
    // Reset log only on first phase (profile fill) — keep log across phase 2
    if (msg.claudeStatus?.stage === 'calling' || msg.claudeStatus?.source === 'none_needed') {
      _activityLog = [];
    }
    renderFields(msg.fillResult, msg.jobCtx, msg.claudeStatus);
  }
  if (msg.type === 'CLAUDE_PROGRESS') {
    updateClaudeProgress(msg);
  }
  if (msg.type === 'AUTOAPPLY_PROGRESS') {
    const prog = document.getElementById('autoApplyProgress');
    if (prog) prog.textContent = msg.text || '';
  }
  if (msg.type === 'AUTOAPPLY_DONE') {
    _autoApplyRunning = false;
    const btn = document.getElementById('btnAutoApply');
    const prog = document.getElementById('autoApplyProgress');
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Auto Apply'; }
    if (prog) prog.textContent = msg.text || 'Done!';
  }
});

// ─── Load previous fill result on open ───────────────────────────────────────

chrome.storage.local.get(['lastFillResult', 'lastJobCtx', 'lastClaudeStatus']).then(({ lastFillResult, lastJobCtx, lastClaudeStatus }) => {
  if (lastFillResult) {
    renderFields(lastFillResult, lastJobCtx, lastClaudeStatus);
  }
});

// Show stats bar and load applied count immediately on open
document.getElementById('statsBar').style.display = '';
refreshAppliedCount();
