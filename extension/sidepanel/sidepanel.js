// Side panel controller

let _currentJobCtx = null;
let _currentFillResult = null;

// ─── Tab navigation ───────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const name = tab.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tabFields').style.display = name === 'fields' ? '' : 'none';
    document.getElementById('tabAts').style.display   = name === 'ats'    ? '' : 'none';
    document.getElementById('tabCover').style.display = name === 'cover'  ? '' : 'none';
  });
});

// ─── Render fill results ──────────────────────────────────────────────────────

function renderFields(fillResult, jobCtx) {
  _currentFillResult = fillResult;
  _currentJobCtx = jobCtx;

  // Header
  if (jobCtx) {
    document.getElementById('jobTitle').textContent = jobCtx.title || 'Job Application';
    document.getElementById('jobCompany').textContent = jobCtx.company || '';
    document.getElementById('jobInfo').style.display = '';
  }

  // Stats bar
  document.getElementById('statFilled').textContent = `${fillResult.filled || 0} filled`;
  document.getElementById('statClaude').textContent = `${fillResult.claude || 0} via Claude`;
  document.getElementById('statSkipped').textContent = `${fillResult.skipped || 0} skipped`;
  document.getElementById('statsBar').style.display = '';

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

  // Load ATS keywords
  loadATS(jobCtx);
}

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[*:()\[\]{}]/g, '')
    .replace(/\s+/g, '_');
}

async function loadATS(jobCtx) {
  if (!jobCtx) return;
  const result = await chrome.runtime.sendMessage({
    type: 'CHECK_ATS',
    jdText: jobCtx.jd_text || '',
  });

  const container = document.getElementById('atsList');
  container.innerHTML = '';
  const keywords = result?.keywords || [];

  if (keywords.length === 0) {
    container.innerHTML = '<div style="color:#80868b;font-size:12px">No technical keywords detected</div>';
    return;
  }

  const present = keywords.filter(k => k.present);
  const missing = keywords.filter(k => !k.present);

  const header = document.createElement('div');
  header.style.cssText = 'font-size:11px;color:#5f6368;margin-bottom:8px';
  header.textContent = `${present.length}/${keywords.length} keywords matched`;
  container.appendChild(header);

  for (const kw of [...present, ...missing]) {
    const row = document.createElement('div');
    row.className = 'kw-row';
    row.innerHTML = `
      <span class="${kw.present ? 'kw-check' : 'kw-miss'}">${kw.present ? '✓' : '✗'}</span>
      <span style="color:${kw.present ? '#202124' : '#80868b'}">${kw.keyword}</span>
    `;
    container.appendChild(row);
  }
}

// ─── Cover letter ─────────────────────────────────────────────────────────────

document.getElementById('btnGenerateCL').addEventListener('click', async () => {
  const btn = document.getElementById('btnGenerateCL');
  btn.textContent = 'Generating…';
  btn.disabled = true;

  const result = await chrome.runtime.sendMessage({
    type: 'GENERATE_COVER_LETTER',
    jobCtx: _currentJobCtx,
  });

  btn.textContent = 'Generate with Claude';
  btn.disabled = false;

  if (result?.text) {
    document.getElementById('coverLetterText').value = result.text;
  } else {
    document.getElementById('coverLetterText').placeholder = 'Claude unavailable. Write your cover letter here.';
  }
});

document.getElementById('btnFillCL').addEventListener('click', async () => {
  const text = document.getElementById('coverLetterText').value.trim();
  if (!text) return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    await chrome.tabs.sendMessage(tab.id, { type: 'FILL_COVER_LETTER', text });
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
    renderFields(msg.fillResult, msg.jobCtx);
  }
});

// ─── Load previous fill result on open ───────────────────────────────────────

chrome.storage.local.get(['lastFillResult', 'lastJobCtx']).then(({ lastFillResult, lastJobCtx }) => {
  if (lastFillResult) {
    renderFields(lastFillResult, lastJobCtx);
  }
});
