// Login wall detector + auto-login / account-creation handler

const LOGIN_SIGNALS = ['sign in', 'log in', 'login', 'create account', 'register', 'sign up', 'create an account'];
const SIGNUP_SIGNALS = ['create account', 'sign up', 'register', 'create an account', 'join now'];
const EMAIL_SELECTORS = ['input[type=email]', 'input[name*=email i]', 'input[id*=email i]', 'input[placeholder*=email i]'];
const PASS_SELECTORS  = ['input[type=password]'];
const FIRST_SELECTORS = ['input[name*=first i]', 'input[id*=first i]', 'input[placeholder*=first i]'];
const LAST_SELECTORS  = ['input[name*=last i]',  'input[id*=last i]',  'input[placeholder*=last i]'];
const NAME_SELECTORS  = ['input[name=name i]',   'input[id*=fullname i]', 'input[placeholder*=full name i]'];
const SUBMIT_SELECTORS = [
  'button[type=submit]', 'input[type=submit]',
  'button:contains("Sign in")', 'button:contains("Log in")',
  'button:contains("Continue")', 'button:contains("Next")',
  '[data-testid*=submit]', '[data-automation-id*=submit]',
];

function loginSleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function fillBySelectors(selectors, value) {
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) {
        el.focus();
        el.value = '';
        const nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
        if (nativeSet && nativeSet.set) nativeSet.set.call(el, value);
        else el.value = value;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        await loginSleep(150);
        return true;
      }
    } catch(e) {}
  }
  return false;
}

async function clickSubmitOrNext() {
  const candidates = [
    'button[type=submit]',
    'input[type=submit]',
    '[data-testid*=submit]',
    '[data-automation-id*=submit]',
  ];
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) {
      el.click();
      return true;
    }
  }
  // Try buttons by text
  const buttons = document.querySelectorAll('button');
  const keywords = ['sign in', 'log in', 'login', 'continue', 'next', 'submit', 'create account', 'register'];
  for (const btn of buttons) {
    const text = (btn.innerText || btn.textContent || '').toLowerCase().trim();
    if (keywords.some(k => text === k || text.startsWith(k))) {
      btn.click();
      return true;
    }
  }
  return false;
}

// Deterministic password — unique per domain + seed
function generatePassword(domain, seed) {
  let hash = 0;
  const str = domain + seed;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash) + str.charCodeAt(i);
    hash |= 0;
  }
  const abs = Math.abs(hash).toString(36).toUpperCase();
  return 'Job' + abs + '!9';
}

async function handleLoginWall(profile, portalAccounts, passwordSeed) {
  const bodyText = document.body.innerText.toLowerCase();
  const isLoginRelated = LOGIN_SIGNALS.some(s => bodyText.includes(s));
  if (!isLoginRelated) return false;

  // Check if there's actually an email/password field on page
  const hasEmailField = EMAIL_SELECTORS.some(s => document.querySelector(s));
  const hasPassField  = PASS_SELECTORS.some(s => document.querySelector(s));
  if (!hasEmailField && !hasPassField) return false;

  const domain = new URL(location.href).hostname;
  const isSignup = SIGNUP_SIGNALS.some(s => bodyText.includes(s));

  // Case 1: have stored credentials → log in
  if (portalAccounts[domain]) {
    const { email, password } = portalAccounts[domain];
    await fillBySelectors(EMAIL_SELECTORS, email);
    await loginSleep(200);
    if (hasPassField) {
      await fillBySelectors(PASS_SELECTORS, password);
      await loginSleep(200);
    }
    await clickSubmitOrNext();
    await loginSleep(2500);
    return 'logged_in';
  }

  // Case 2: no credentials + signup form → create account
  if (isSignup) {
    const password = generatePassword(domain, passwordSeed || 'jobberai');
    await fillBySelectors(EMAIL_SELECTORS, profile.email);
    await loginSleep(150);
    await fillBySelectors(PASS_SELECTORS, password);
    await loginSleep(150);
    // Fill name if present
    const hasFirstField = FIRST_SELECTORS.some(s => document.querySelector(s));
    if (hasFirstField) {
      await fillBySelectors(FIRST_SELECTORS, profile.first_name);
      await fillBySelectors(LAST_SELECTORS, profile.last_name);
    } else {
      await fillBySelectors(NAME_SELECTORS, profile.full_name);
    }
    await loginSleep(200);
    await clickSubmitOrNext();

    // Save credentials via message to service worker
    await chrome.runtime.sendMessage({
      type: 'SAVE_PORTAL_ACCOUNT',
      domain,
      email: profile.email,
      password,
    });
    await loginSleep(2500);
    return 'account_created';
  }

  // Case 3: login form, no stored creds — try profile email + common passwords
  // (user may have registered manually before)
  await fillBySelectors(EMAIL_SELECTORS, profile.email);
  await loginSleep(150);
  const password = generatePassword(domain, passwordSeed || 'jobberai');
  if (hasPassField) {
    await fillBySelectors(PASS_SELECTORS, password);
    await loginSleep(150);
  }
  await clickSubmitOrNext();
  await loginSleep(2500);

  // Save speculatively
  await chrome.runtime.sendMessage({
    type: 'SAVE_PORTAL_ACCOUNT',
    domain,
    email: profile.email,
    password,
  });
  return 'login_attempted';
}
