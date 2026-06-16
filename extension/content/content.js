// Main orchestrator — runs in page context, wires autofill + login + Claude

let _currentJobCtx = null;
let _autoApplyStopped = false;

function scrapeJobContext() {
  const titleSelectors = [
    'h1.posting-headline', '.job-title', 'h1[data-testid*="title"]',
    '.posting-title', '[data-testid="job-title"]', '.app-title',
    'h1.jobsearch-JobInfoHeader-title', 'h1',
  ];
  const companySelectors = [
    '.company-name', '.employer-name', 'h2.company',
    '[data-company]', '.posting-company', '.company',
    'h1.jobsearch-CompanyInfoHeaderDesktop-companyNameSimpleLink',
    '[data-testid="inlineHeader-companyName"]',
  ];

  function firstText(sels) {
    for (const s of sels) {
      try {
        const el = document.querySelector(s);
        if (el) {
          const t = (el.innerText || el.textContent || '').trim();
          if (t) return t;
        }
      } catch(e) {}
    }
    return '';
  }

  return {
    title:   firstText(titleSelectors)   || document.title,
    company: firstText(companySelectors) || new URL(location.href).hostname,
    jd_text: document.body.innerText.slice(0, 4000),
    url:     location.href,
  };
}

async function clickNext() {
  const nextSelectors = [
    'button[type=submit]',
    '[data-testid*=next]', '[data-automation-id*=next]',
    '[aria-label*="Next" i]', '[aria-label*="Continue" i]',
  ];
  const keywords = ['next', 'continue', 'proceed', 'forward', 'save and continue'];

  for (const sel of nextSelectors) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) { el.click(); return true; }
  }
  const buttons = document.querySelectorAll('button');
  for (const btn of buttons) {
    const text = (btn.innerText || '').toLowerCase().trim();
    if (keywords.some(k => text === k || text.startsWith(k))) { btn.click(); return true; }
  }
  return false;
}

async function clickSubmit() {
  const submitKeywords = ['submit', 'submit application', 'apply', 'send application', 'complete application'];
  const buttons = document.querySelectorAll('button, input[type=submit]');
  for (const btn of buttons) {
    const text = (btn.innerText || btn.value || '').toLowerCase().trim();
    if (submitKeywords.some(k => text === k || text.includes(k))) { btn.click(); return true; }
  }
  // fallback
  const el = document.querySelector('button[type=submit], input[type=submit]');
  if (el) { el.click(); return true; }
  return false;
}

function showStatus(msg, type = 'info') {
  const colors = { info: '#1a73e8', success: '#0f9d58', error: '#d93025', warn: '#f57c00' };
  const existing = document.getElementById('jobberai-status');
  if (existing) existing.remove();

  const div = document.createElement('div');
  div.id = 'jobberai-status';
  div.style.cssText = `
    position: fixed; top: 16px; right: 16px; z-index: 2147483647;
    background: ${colors[type] || colors.info}; color: white;
    padding: 10px 16px; border-radius: 8px; font-size: 14px;
    font-family: -apple-system, sans-serif; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    max-width: 320px; line-height: 1.4;
  `;
  div.textContent = 'JobberAI: ' + msg;
  document.body.appendChild(div);
  setTimeout(() => div.remove(), 5000);
}

// Long-lived port call — keeps service worker alive for 60-90s Claude calls
function getAnswersViaPort(fields, jobCtx) {
  return new Promise((resolve) => {
    let port;
    try {
      port = chrome.runtime.connect({ name: 'claude_request' });
    } catch(e) {
      resolve({ ok: false, error: 'Port connect failed: ' + e.message, answers: {} });
      return;
    }
    const timeout = setTimeout(() => {
      try { port.disconnect(); } catch(e) {}
      resolve({ ok: false, error: 'Claude timed out after 120s', source: 'timeout', answers: {} });
    }, 120000);

    port.onMessage.addListener((msg) => {
      if (msg.type === 'GET_ANSWERS_RESULT') {
        clearTimeout(timeout);
        try { port.disconnect(); } catch(e) {}
        resolve(msg.result);
      }
    });
    port.onDisconnect.addListener(() => {
      clearTimeout(timeout);
      resolve({ ok: false, error: 'Service worker disconnected', source: 'sw_disconnect', answers: {} });
    });
    port.postMessage({ type: 'GET_ANSWERS_VIA_PORT', fields, jobCtx });
  });
}

// Main autofill handler
chrome.runtime.onMessage.addListener(async (msg, sender, sendResponse) => {
  if (msg.type === 'AUTOFILL_START') {
    (async () => {
      try {
        showStatus('Starting autofill...', 'info');

        const stored = await chrome.storage.local.get(['profile', 'portal_accounts', 'memory', 'password_seed']);
        const profile = stored.profile || {};
        const portalAccounts = stored.portal_accounts || {};
        const memory = stored.memory || {};
        const passwordSeed = stored.password_seed || 'jobberai2024';

        // Step 1: Handle login wall
        const loginResult = await handleLoginWall(profile, portalAccounts, passwordSeed);
        if (loginResult === 'account_created') {
          showStatus('Account created! Checking email verification...', 'info');
          const domain = new URL(location.href).hostname;
          await checkEmailVerification(domain, 60000);
          await new Promise(r => setTimeout(r, 1500));
        } else if (loginResult === 'logged_in') {
          showStatus('Logged in!', 'success');
          await new Promise(r => setTimeout(r, 1500));
        }

        // Step 2: Scrape job context
        _currentJobCtx = scrapeJobContext();

        // Step 3: Detect fields
        const fields = detectFormFields();
        showStatus(`Detected ${fields.length} fields, filling from profile...`, 'info');

        // Step 4a: Immediate profile+memory fill (no Claude wait)
        const fillResult = await autofillPage(profile, memory, null);

        showStatus(`${fillResult.filled} filled from profile. Asking Claude for the rest...`, 'info');

        // Step 4b: Show partial results in sidepanel immediately
        const pendingStatus = fillResult.needsClaude?.length > 0
          ? { ok: null, error: null, source: 'calling', fieldsCount: fillResult.needsClaude.length, reviewedBy: null, stage: 'calling' }
          : { ok: true, error: null, source: 'none_needed', fieldsCount: 0, reviewedBy: 'none' };
        chrome.runtime.sendMessage({
          type: 'FILL_COMPLETE',
          fillResult,
          jobCtx: _currentJobCtx,
          claudeStatus: pendingStatus,
        });

        // Step 5: Claude fills remaining fields
        let claudeStatus = { ok: null, error: null, source: null, fieldsCount: 0, reviewedBy: null };

        if (fillResult.needsClaude && fillResult.needsClaude.length > 0) {
          showStatus(`Sending ${fillResult.needsClaude.length} fields to Claude...`, 'info');

          let result;
          try {
            result = await getAnswersViaPort(fillResult.needsClaude, _currentJobCtx);
          } catch(e) {
            result = { ok: false, answers: null, error: 'Message send failed: ' + e.message, source: 'exception', fieldsCount: fillResult.needsClaude.length, reviewedBy: 'none' };
          }

          claudeStatus = result || { ok: false, answers: null, error: 'No response from service worker', source: 'no_response', fieldsCount: fillResult.needsClaude.length, reviewedBy: 'none' };

          if (result && result.ok && result.answers && Object.keys(result.answers).length > 0) {
            for (const f of fillResult.needsClaude) {
              const key = normalizeLabel(f.label_text);
              const ans = result.answers[key] || result.answers[f.label_text];
              if (ans) {
                const ok = await fillField(f, ans);
                if (ok) {
                  fillResult.claude++;
                  fillResult.filled++;
                  fillResult.fields.push({ label: f.label_text, value: ans, source: 'claude' });
                }
              }
            }
            showStatus(`Done! ${fillResult.filled} filled (${fillResult.claude} via Claude, reviewed by ${result.reviewedBy}).`, 'success');
          } else {
            const errMsg = result?.error || 'No answers returned';
            showStatus(`Claude failed: ${errMsg}`, 'error');
          }
        } else {
          claudeStatus = { ok: true, error: null, source: 'none_needed', fieldsCount: 0, reviewedBy: 'none' };
          showStatus(`Done! ${fillResult.filled} filled from profile.`, 'success');
        }

        // Final sidepanel update with full status
        chrome.runtime.sendMessage({
          type: 'FILL_COMPLETE',
          fillResult,
          jobCtx: _currentJobCtx,
          claudeStatus,
        });

      } catch(err) {
        console.error('JobberAI: autofill error', err);
        showStatus('Autofill error: ' + err.message, 'error');
      }
    })();
    return true;
  }

  if (msg.type === 'AUTOAPPLY_STOP') {
    _autoApplyStopped = true;
    showStatus('Auto Apply stopped.', 'warn');
    chrome.runtime.sendMessage({ type: 'AUTOAPPLY_DONE', text: 'Stopped by user.' });
    return true;
  }

  if (msg.type === 'AUTOAPPLY_START') {
    _autoApplyStopped = false;
    (async () => {
      let pageNum = 1;
      const MAX_PAGES = 15;
      let submitted = false;

      function progress(text) {
        showStatus(text, 'info');
        chrome.runtime.sendMessage({ type: 'AUTOAPPLY_PROGRESS', text });
      }

      // Fill current page with profile → Claude pipeline. Returns claudeAnswers for retry use.
      async function fillCurrentPage() {
        const stored = await chrome.storage.local.get(['profile', 'memory']);
        const profile = stored.profile || {};
        const memory = stored.memory || {};

        _currentJobCtx = scrapeJobContext();
        const fillResult = await autofillPage(profile, memory, null);
        progress(`Page ${pageNum}: ${fillResult.filled} filled from profile. Asking Claude…`);

        const pendingStatus = fillResult.needsClaude?.length > 0
          ? { ok: null, source: 'calling', fieldsCount: fillResult.needsClaude.length, stage: 'calling' }
          : { ok: true, source: 'none_needed', fieldsCount: 0 };
        chrome.runtime.sendMessage({ type: 'FILL_COMPLETE', fillResult, jobCtx: _currentJobCtx, claudeStatus: pendingStatus });

        let claudeAnswers = null;
        if (fillResult.needsClaude?.length > 0) {
          let result;
          try {
            result = await getAnswersViaPort(fillResult.needsClaude, _currentJobCtx);
          } catch(e) {
            result = { ok: false, error: e.message };
          }
          if (result?.ok && result.answers) {
            claudeAnswers = result.answers;
            for (const f of fillResult.needsClaude) {
              const ans = result.answers[normalizeLabel(f.label_text)] || result.answers[f.label_text];
              if (ans) {
                const ok = await fillField(f, ans);
                if (ok) { fillResult.claude++; fillResult.filled++; fillResult.fields.push({ label: f.label_text, value: ans, source: 'claude' }); }
              }
            }
          }
          chrome.runtime.sendMessage({ type: 'FILL_COMPLETE', fillResult, jobCtx: _currentJobCtx, claudeStatus: result || pendingStatus });
        }

        progress(`Page ${pageNum}: ${fillResult.filled} filled (${fillResult.claude} via Claude). Verifying DOM…`);
        return { fillResult, claudeAnswers, profile, memory };
      }

      // Re-scan DOM for empty fields; retry any that have an answer; return still-empty required fields.
      async function verifyAndRetryEmpty(profile, memory, claudeAnswers) {
        const fields = detectFormFields();
        const stillEmpty = [];
        let retried = 0;

        for (const field of fields) {
          if (field.field_type === 'file') continue; // can't programmatically verify file inputs
          if (field.field_type === 'aria_radio') continue; // hard to verify; assume filled

          const domVal = getFieldDomValue(field);
          if (domVal === null || domVal !== '') continue; // not in DOM or already has a value

          // Field is empty — find an answer and retry the fill
          const ans = resolveAnswerForRetry(field, profile, memory, claudeAnswers);
          if (ans && ans !== '_needs_claude') {
            await fillField(field, ans);
            await sleep(350); // let React settle
            const afterVal = getFieldDomValue(field);
            if (afterVal !== null && afterVal !== '') {
              retried++;
              continue;
            }
          }

          // Still empty after retry (or no answer found)
          if (isFieldRequired(field)) {
            stillEmpty.push(field.label_text);
          }
        }

        return { stillEmpty, retried };
      }

      try {
        for (let i = 0; i < MAX_PAGES; i++) {
          if (_autoApplyStopped) { progress('Stopped.'); break; }
          const { fillResult, claudeAnswers, profile, memory } = await fillCurrentPage();

          // Let React finish re-rendering before inspecting DOM values
          await sleep(500);
          const { stillEmpty, retried } = await verifyAndRetryEmpty(profile, memory, claudeAnswers);

          if (retried > 0) progress(`Page ${pageNum}: retried ${retried} empty field(s) — all good now.`);
          if (stillEmpty.length > 0) {
            progress(`Page ${pageNum}: ⚠ ${stillEmpty.length} required field(s) still empty: ${stillEmpty.slice(0, 4).join(', ')}${stillEmpty.length > 4 ? '…' : ''}`);
          }

          // Check for submit button
          const submitKeywords = ['submit', 'submit application', 'apply', 'send application', 'complete application'];
          const allBtns = Array.from(document.querySelectorAll('button, input[type=submit]'));
          const submitBtn = allBtns.find(btn =>
            !btn.disabled &&
            submitKeywords.some(k => (btn.innerText || btn.value || '').toLowerCase().trim().includes(k))
          );

          if (submitBtn) {
            if (stillEmpty.length > 0) {
              const msg2 = `Blocked submit — fill these required field(s) manually: ${stillEmpty.join(', ')}`;
              progress(`⛔ ${msg2}`);
              chrome.runtime.sendMessage({ type: 'AUTOAPPLY_DONE', text: msg2 });
              showStatus(msg2, 'warn');
              break;
            }
            // AUTO-SUBMIT DISABLED — highlight submit button and let user click manually
            submitBtn.style.outline = '3px solid #34a853';
            submitBtn.style.boxShadow = '0 0 8px #34a853';
            progress(`✅ All fields filled — click Submit ▶ in the sidepanel or the highlighted button to submit.`);
            chrome.runtime.sendMessage({ type: 'AUTOAPPLY_DONE', text: 'Ready to submit — review and click Submit.' });
            showStatus('Ready — review fields then submit manually.', 'success');
            break;
          }

          // Try next button
          const clicked = await clickNext();
          if (!clicked) {
            chrome.runtime.sendMessage({ type: 'AUTOAPPLY_DONE', text: `Stopped at page ${pageNum} — no Next/Submit found.` });
            showStatus('Auto Apply: no Next or Submit found.', 'warn');
            break;
          }
          pageNum++;
          progress(`Navigated to page ${pageNum}, waiting for form…`);
          await sleep(2200);
        }
        if (!submitted && pageNum >= MAX_PAGES) {
          chrome.runtime.sendMessage({ type: 'AUTOAPPLY_DONE', text: `Stopped after ${MAX_PAGES} pages.` });
        }
      } catch(err) {
        chrome.runtime.sendMessage({ type: 'AUTOAPPLY_DONE', text: `Error: ${err.message}` });
        showStatus('Auto Apply error: ' + err.message, 'error');
      }
    })();
    return true;
  }

  if (msg.type === 'NEXT_PAGE') {
    (async () => {
      const clicked = await clickNext();
      if (clicked) {
        await new Promise(r => setTimeout(r, 1800));
        // Re-trigger autofill on the new page
        chrome.runtime.sendMessage({ type: 'AUTOFILL_START' });
      }
    })();
    return true;
  }

  if (msg.type === 'SUBMIT_FORM') {
    (async () => {
      await clickSubmit();
      chrome.runtime.sendMessage({
        type: 'LOG_APPLICATION',
        jobCtx: _currentJobCtx || scrapeJobContext(),
        timestamp: new Date().toISOString(),
      });
      showStatus('Application submitted!', 'success');
    })();
    return true;
  }

  if (msg.type === 'GET_FIELDS') {
    sendResponse({ fields: detectFormFields() });
    return true;
  }

  // Manual field fill from sidepanel edit — find field by label and apply value to DOM
  if (msg.type === 'FILL_FIELD') {
    (async () => {
      const fields = detectFormFields();
      const lbl = (msg.label || '').toLowerCase().trim();
      const target = fields.find(f =>
        f.label_text === lbl ||
        normalizeLabel(f.label_text) === normalizeLabel(lbl)
      );
      if (target) {
        await fillField(target, msg.value);
      } else if (msg.selector) {
        // Fallback: try direct selector
        try {
          const el = document.querySelector(msg.selector);
          if (el) await _fillText(el, msg.value);
        } catch(e) {}
      }
    })();
    return true;
  }

  if (msg.type === 'FILL_OTP') {
    // Fill OTP/verification code into the current page
    (async () => {
      const inputs = document.querySelectorAll('input[type=text], input[type=number], input[autocomplete*=one-time]');
      for (const inp of inputs) {
        if (inp.maxLength >= 4 && inp.maxLength <= 8 || inp.placeholder.toLowerCase().includes('code')) {
          inp.value = msg.code;
          inp.dispatchEvent(new InputEvent('input', { bubbles: true }));
          inp.dispatchEvent(new Event('change', { bubbles: true }));
          await clickNext();
          break;
        }
      }
    })();
    return true;
  }
});
