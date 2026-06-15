// Side panel controller

let _currentJobCtx = null;
let _currentFillResult = null;

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
}

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[*:()\[\]{}]/g, '')
    .replace(/\s+/g, '_');
}

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
