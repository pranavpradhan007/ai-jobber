// Popup controller

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function updateStats() {
  const { lastFillResult, applications = [] } = await chrome.storage.local.get(['lastFillResult', 'applications']);
  if (lastFillResult) {
    document.getElementById('statFilled').textContent = lastFillResult.filled || 0;
    document.getElementById('statClaude').textContent = lastFillResult.claude || 0;
  }
  document.getElementById('statApplied').textContent = applications.length;
}

async function checkApiKey() {
  // Try pinging the local proxy to see if it's running
  try {
    const resp = await fetch('http://localhost:3747/v1/messages', {
      method: 'OPTIONS',
      signal: AbortSignal.timeout(800),
    });
    // Proxy is up — no warning needed
    document.getElementById('statusText').textContent = 'Proxy connected';
  } catch(e) {
    // Proxy not running — check if direct key is set
    const { claudeApiKey } = await chrome.storage.local.get('claudeApiKey');
    if (!claudeApiKey) {
      document.getElementById('apiWarning').style.display = 'block';
      document.getElementById('statusDot').className = 'status-dot orange';
      document.getElementById('statusText').textContent = 'Run start_proxy.bat first';
    }
  }
}

async function checkRateLimit() {
  const { claudeRateLimited, claudeRateLimitedAt } = await chrome.storage.local.get(
    ['claudeRateLimited', 'claudeRateLimitedAt']
  );
  if (claudeRateLimited && Date.now() - claudeRateLimitedAt < 3600 * 1000) {
    document.getElementById('statusDot').className = 'status-dot orange';
    document.getElementById('statusText').textContent = 'Claude rate-limited — fallback mode';
  }
}

document.getElementById('btnAutofill').addEventListener('click', async () => {
  const tab = await getActiveTab();
  if (!tab) return;

  const btn = document.getElementById('btnAutofill');
  const spinner = document.getElementById('spinner');
  const btnText = document.getElementById('btnText');
  btn.disabled = true;
  spinner.style.display = 'block';
  btnText.textContent = 'Filling…';
  document.getElementById('statusDot').className = 'status-dot orange';
  document.getElementById('statusText').textContent = 'Autofilling…';

  try {
    await chrome.tabs.sendMessage(tab.id, { type: 'AUTOFILL_START' });
  } catch(e) {
    document.getElementById('statusText').textContent = 'Error: reload the page and try again';
    document.getElementById('statusDot').className = 'status-dot red';
  }

  setTimeout(() => {
    btn.disabled = false;
    spinner.style.display = 'none';
    btnText.textContent = '✨ Autofill';
    updateStats();
  }, 3000);
});

document.getElementById('btnPanel').addEventListener('click', async () => {
  const tab = await getActiveTab();
  if (tab) await chrome.sidePanel.open({ tabId: tab.id });
  window.close();
});

document.getElementById('goOptions').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
  window.close();
});

document.getElementById('goSettings').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
  window.close();
});

document.getElementById('goTracker').addEventListener('click', () => {
  chrome.tabs.create({ url: chrome.runtime.getURL('options/options.html#tracker') });
  window.close();
});

// Init
updateStats();
checkApiKey();
checkRateLimit();
