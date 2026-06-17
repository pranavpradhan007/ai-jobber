// Service worker — Claude API, message routing, memory, Gmail proxy

// Local proxy (reads ANTHROPIC_API_KEY from .env — same key Claude Code uses)
// Run extension/start_proxy.bat to start it before using the extension.
const PROXY_URL = 'http://localhost:3747/v1/messages';
const DIRECT_API_URL = 'https://api.anthropic.com/v1/messages';
const CLAUDE_MODEL = 'claude-sonnet-4-6';
const RATE_LIMIT_TTL = 3600 * 1000; // 1 hour

// ─── Claude API ─────────────────────────────────────────────────────────────

async function getApiKey() {
  // Returns key only if user manually set one in options (direct mode).
  // Normally null — proxy mode is used instead.
  const { claudeApiKey } = await chrome.storage.local.get('claudeApiKey');
  return claudeApiKey || null;
}

async function callClaude(apiKey, systemPrompt, userPrompt, maxTokens = 1500) {
  // If a key is stored in extension, call Anthropic directly.
  // Otherwise route through local proxy (uses Claude Code's API key from .env).
  const useProxy = !apiKey;
  const url = useProxy ? PROXY_URL : DIRECT_API_URL;

  const headers = { 'Content-Type': 'application/json' };
  if (!useProxy) {
    headers['x-api-key'] = apiKey;
    headers['anthropic-version'] = '2023-06-01';
  }

  const resp = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: maxTokens,
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
    }),
    signal: AbortSignal.timeout(100000), // 100s — proxy subprocess times out at 90s
  });

  if (resp.status === 429) {
    const err = new Error('rate_limited');
    err.status = 429;
    throw err;
  }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Claude API error ${resp.status}: ${text.slice(0, 200)}`);
  }
  const data = await resp.json();
  return data.content?.[0]?.text || '';
}

function parseBatchResponse(text, fields) {
  // Extract JSON from Claude response
  const jsonMatch = text.match(/```json\s*([\s\S]*?)\s*```/) ||
                    text.match(/(\{[\s\S]*\})/);
  if (!jsonMatch) return {};
  try {
    return JSON.parse(jsonMatch[1]);
  } catch(e) {
    return {};
  }
}

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[^\w\s]/g, '')   // strip ALL non-word, non-space chars (handles ✱ ❋ ＊ etc.)
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .trim();
}

// ─── Prompt builders ──────────────────────────────────────────────────────────

function buildResumeBlock(profile) {
  const lines = [];
  lines.push(`${profile.full_name || ''} | ${profile.email || ''} | ${profile.phone || ''} | ${profile.location || ''}`);
  lines.push(`${profile.current_title || ''} at ${profile.current_company || ''}`);
  lines.push(`${profile.education_degree || ''}, ${profile.education_school || ''} (${profile.graduation_year || ''})`);
  lines.push(`Skills: ${(profile.skills_short || profile.skills || '').slice(0, 500)}`);
  const wh = (profile.work_history || []).slice(0, 6)
    .map(w => `  ${w.title} @ ${w.company} (${w.start_date || ''}–${w.end_date || 'present'}): ${(w.description || '').slice(0, 200)}`);
  if (wh.length) lines.push('Experience:\n' + wh.join('\n'));
  return lines.join('\n');
}

function buildDraftPrompt(fields, jobCtx, profile) {
  const resumeBlock = buildResumeBlock(profile);

  const fieldsList = fields.map(f => {
    const opts = f.radio_options?.length ? ` (options: ${f.radio_options.join(' | ')})` :
                 f.select_options?.length ? ` (options: ${f.select_options.slice(0, 8).join(' | ')})` : '';
    return `"${normalizeLabel(f.label_text)}": "..."  // ${f.label_text}${opts}`;
  }).join('\n');

  const jdSection = jobCtx.jd_text
    ? `\nJob description:\n${jobCtx.jd_text.slice(0, 2000)}`
    : '';

  return {
    system: `You are filling a job application. Return ONLY valid JSON {"field_key":"answer"}. No markdown fences, no explanation. Never invent facts not in the resume.`,
    user: `Position: ${jobCtx.company} — ${jobCtx.title}

Resume:
${resumeBlock}
${jdSection}

Rules:
1. Use exact values from the resume for contact/location fields
2. Open-ended answers: under 100 words, first person, specific projects/tools
3. No AI buzzwords (passionate, leverage, delve, synergy, thrilled, utilize, robust, cutting-edge)
4. No em dashes. Sound like a real engineer, not a polished template
5. Yes/No: answer from resume facts only

Fields to fill:
${fieldsList}

JSON:`
  };
}

const AI_BANNED_WORDS = [
  'passionate', 'excited', 'leverage', 'delve', 'thrilled', 'honored',
  'I am pleased', 'I am eager', 'dynamic', 'synergy', 'spearhead',
  'cutting-edge', 'world-class', 'utilize', 'facilitate', 'robust',
  'transformative', 'impactful', 'seamlessly', 'empower', 'humbly',
  'I am excited to', 'I am passionate', 'it is my pleasure',
];

function buildReviewPrompt(draftAnswers, fields, jobCtx, profile) {
  const profileFacts = {
    name: profile.full_name,
    title: profile.current_title,
    school: profile.education_school,
    companies: (profile.work_history || []).map(w => w.company),
    projects: (profile.work_history || []).map(w => w.description).join(' '),
  };

  return {
    system: `You are reviewing job application answers for ${jobCtx.company} — ${jobCtx.title}.
The candidate is a recent grad from India. Answers should sound human and slightly informal, not like AI.
Return ONLY valid JSON with the same keys. Keep good answers. Rewrite bad ones.`,
    user: `Profile facts (ONLY reference these, no invented facts):
${JSON.stringify(profileFacts, null, 2)}

Draft answers:
${JSON.stringify(draftAnswers, null, 2)}

Rewrite any answer that:
- Uses banned AI words: ${AI_BANNED_WORDS.slice(0, 10).join(', ')} or similar corporate language
- Contains em dashes (—) — replace with commas or split into two sentences
- Opens with "I am passionate/excited/thrilled"
- Invents facts not in the profile above
- Is over 120 words for an open-ended field
- Sounds like a template, not a real person

Style rules for rewrites:
- Slightly imperfect grammar is fine and preferred over robotic perfection
- Be specific: name actual projects, companies, numbers from the profile
- No em dashes anywhere
- Short sentences over long complex ones

Return JSON: { "field_key": "answer", ... }`
  };
}

// ─── Auto Reviewer (no Claude needed) ────────────────────────────────────────

function extractProfileFacts(profile) {
  const text = JSON.stringify(profile);
  const proper = text.match(/[A-Z][a-zA-Z]{2,}/g) || [];
  return [...new Set(proper)].filter(w => w.length > 3);
}

function fuzzyMatchOption(value, options) {
  const target = value.toLowerCase().trim();
  // Exact match
  let m = options.find(o => o.toLowerCase().trim() === target);
  if (m) return m;
  // Contains match
  m = options.find(o => o.toLowerCase().includes(target) || target.includes(o.toLowerCase()));
  return m || null;
}

function autoReview(answers, fields, profile) {
  const PROFILE_FACTS = extractProfileFacts(profile);
  const CONTACT_KEYS = ['email', 'phone', 'full_name', 'first_name', 'last_name', 'address_zip', 'address_city', 'address_state'];

  const result = {};
  for (const [key, ans] of Object.entries(answers)) {
    if (!ans || typeof ans !== 'string') { result[key] = ans; continue; }
    let reviewed = ans;

    // Rule 0: Strip em dashes always
    reviewed = reviewed.replace(/—/g, ',').replace(/--/g, ',');

    // Rule 1: Generic AI opener → replace with something specific
    if (/^I am (passionate|excited|thrilled|honored|pleased|eager)/i.test(reviewed) && PROFILE_FACTS.length > 0) {
      const specificFact = PROFILE_FACTS.find(f => f.length > 4) || PROFILE_FACTS[0];
      reviewed = reviewed.replace(
        /^I am (passionate|excited|thrilled|honored|pleased|eager)\s*(about|to|for)?\s*/i,
        `Having worked on ${specificFact}, `
      );
    }

    // Rule 2: Word count — truncate if > 150 words
    const words = reviewed.split(/\s+/);
    if (words.length > 150) reviewed = words.slice(0, 140).join(' ') + '…';

    // Rule 3: Contact info fields — always exact profile value
    if (CONTACT_KEYS.includes(key) && profile[key]) reviewed = profile[key];

    // Rule 4: Select/radio — fuzzy match to valid options
    const field = fields.find(f => normalizeLabel(f.label_text) === key);
    if (field && field.select_options && field.select_options.length > 0) {
      const match = fuzzyMatchOption(reviewed, field.select_options);
      if (match) reviewed = match;
    }
    if (field && field.radio_options && field.radio_options.length > 0) {
      const match = fuzzyMatchOption(reviewed, field.radio_options);
      if (match) reviewed = match;
    }

    result[key] = reviewed;
  }
  return result;
}

// ─── Rate limit management ────────────────────────────────────────────────────

async function isRateLimited() {
  const { claudeRateLimited, claudeRateLimitedAt } = await chrome.storage.local.get(
    ['claudeRateLimited', 'claudeRateLimitedAt']
  );
  return claudeRateLimited && (Date.now() - claudeRateLimitedAt < RATE_LIMIT_TTL);
}

async function setRateLimited() {
  await chrome.storage.local.set({ claudeRateLimited: true, claudeRateLimitedAt: Date.now() });
}

async function clearRateLimited() {
  await chrome.storage.local.set({ claudeRateLimited: false });
}

// ─── Learning memory ──────────────────────────────────────────────────────────

const STATIC_PROFILE_KEYS = new Set([
  'full_name', 'first_name', 'last_name', 'email', 'phone', 'phone_country_code',
  'linkedin_url', 'github_url', 'portfolio_url', 'address_city', 'address_state',
  'address_zip', 'address_country', 'address_line1', 'salary_expectation',
  'years_experience', 'education_degree', 'graduation_year', 'work_authorization',
  'requires_sponsorship', 'us_citizen_or_pr', 'remote_preference', 'willing_to_relocate',
  'eeo_gender', 'eeo_ethnicity', 'eeo_veteran', 'eeo_disability',
]);

async function learnFromAnswers(answers, fields) {
  const { memory = {} } = await chrome.storage.local.get('memory');
  const today = new Date().toISOString().slice(0, 10);

  for (const f of fields) {
    const key = normalizeLabel(f.label_text);
    const ans = answers[key];
    if (!ans || typeof ans !== 'string' || ans.length < 2) continue;
    if (STATIC_PROFILE_KEYS.has(key)) continue; // don't memorize static profile fields
    // Don't cache EEO "decline/prefer not" answers — they pollute future fills
    if (/prefer not|decline|not wish to answer/i.test(ans)) continue;
    // Don't memorize UUID-based Lever survey field names (cards[uuid][field0]).
    // These keys are form-instance-specific and will never match on another page.
    if (/cards[0-9a-f]{8,}field\d+/i.test(key)) continue;

    if (!memory[key]) {
      memory[key] = {
        answer: ans,
        label: f.label_text,
        source: 'claude',
        seen: 1,
        last_seen: today,
      };
    } else {
      memory[key].seen = (memory[key].seen || 0) + 1;
      memory[key].last_seen = today;
      // Update if Claude provided a better answer (longer/more specific)
      if (ans.length > (memory[key].answer || '').length) {
        memory[key].answer = ans;
        memory[key].source = 'claude_updated';
      }
    }
  }

  await chrome.storage.local.set({ memory });
}

// ─── Progress broadcast ───────────────────────────────────────────────────────

function broadcastProgress(data) {
  // Fire-and-forget to all extension pages (sidepanel, popup).
  // Ignore errors — sidepanel may not be open yet.
  chrome.runtime.sendMessage({ type: 'CLAUDE_PROGRESS', ...data }).catch(() => {});
}

// ─── Three-agent pipeline ──────────────────────────────────────────────────────

// Returns { ok, answers, error, stage, fieldsCount, reviewedBy }
async function getLLMAnswers(apiKey, fields, jobCtx, profile) {
  const claudeFields = fields.filter(f => !STATIC_PROFILE_KEYS.has(normalizeLabel(f.label_text)));
  if (claudeFields.length === 0) return { ok: true, answers: {}, fieldsCount: 0, stage: 'skipped', reviewedBy: 'none' };

  // Agent 1 — Drafter
  broadcastProgress({ stage: 'drafter_start', fieldsCount: claudeFields.length, fields: claudeFields.map(f => f.label_text) });

  let draftRaw;
  try {
    const { system: draftSys, user: draftUser } = buildDraftPrompt(claudeFields, jobCtx, profile);
    draftRaw = await callClaude(apiKey, draftSys, draftUser, 800);
  } catch(err) {
    const msg = err.status === 429
      ? 'Rate limited by Claude API (resets in ~1 h)'
      : `Drafter failed: ${err.message}`;
    if (err.status === 429) await setRateLimited();
    broadcastProgress({ stage: 'error', error: msg });
    return { ok: false, answers: {}, error: msg, stage: 'drafter', fieldsCount: claudeFields.length, reviewedBy: 'none' };
  }

  const draftAnswers = parseBatchResponse(draftRaw, claudeFields);
  if (!draftAnswers || Object.keys(draftAnswers).length === 0) {
    const errMsg = `Drafter returned no parseable JSON. Raw: ${draftRaw.slice(0, 200)}`;
    broadcastProgress({ stage: 'error', error: errMsg });
    return { ok: false, answers: {}, error: errMsg, stage: 'drafter_parse', fieldsCount: claudeFields.length, reviewedBy: 'none' };
  }

  broadcastProgress({ stage: 'drafter_done', answersCount: Object.keys(draftAnswers).length, preview: draftAnswers });

  // Agent 2 — Reviewer
  let finalAnswers = draftAnswers;
  let reviewedBy = 'auto';
  const rateLimited = await isRateLimited();

  if (!rateLimited) {
    broadcastProgress({ stage: 'reviewer_start' });
    try {
      const { system: revSys, user: revUser } = buildReviewPrompt(draftAnswers, claudeFields, jobCtx, profile);
      const reviewRaw = await callClaude(apiKey, revSys, revUser, 800);
      const reviewed = parseBatchResponse(reviewRaw, claudeFields);
      if (reviewed && Object.keys(reviewed).length > 0) {
        finalAnswers = reviewed;
        reviewedBy = 'claude';
      } else {
        finalAnswers = autoReview(draftAnswers, claudeFields, profile);
        reviewedBy = 'auto (reviewer returned no JSON)';
      }
    } catch(err) {
      if (err.status === 429) await setRateLimited();
      finalAnswers = autoReview(draftAnswers, claudeFields, profile);
      reviewedBy = `auto (reviewer error: ${err.message})`;
    }
    broadcastProgress({ stage: 'reviewer_done', reviewedBy, answersCount: Object.keys(finalAnswers).length });
  } else {
    finalAnswers = autoReview(draftAnswers, claudeFields, profile);
    reviewedBy = 'auto (rate limited)';
    broadcastProgress({ stage: 'reviewer_done', reviewedBy, answersCount: Object.keys(finalAnswers).length });
  }

  await learnFromAnswers(finalAnswers, claudeFields);
  return { ok: true, answers: finalAnswers, fieldsCount: claudeFields.length, stage: 'done', reviewedBy };
}

// Returns { ok, answers, error, source, fieldsCount, reviewedBy }
async function getAnswers(fields, jobCtx) {
  const { profile = {} } = await chrome.storage.local.get('profile');
  const { memory = {} } = await chrome.storage.local.get('memory');

  const rateLimited = await isRateLimited();
  if (rateLimited) {
    const { claudeRateLimitedAt } = await chrome.storage.local.get('claudeRateLimitedAt');
    const mins = Math.ceil((RATE_LIMIT_TTL - (Date.now() - claudeRateLimitedAt)) / 60000);
    return { ok: false, answers: null, error: `Rate limited — resets in ~${mins} min`, source: 'rate_limited', fieldsCount: 0, reviewedBy: 'none' };
  }

  const needsLLM = fields.filter(f => {
    const key = normalizeLabel(f.label_text);
    if (STATIC_PROFILE_KEYS.has(key)) return false;
    // If memory has a "decline/prefer not" answer, still send to Claude for a real answer
    const memAns = memory[key]?.answer;
    if (memAns && /prefer not|decline|not wish to answer/i.test(String(memAns))) return true;
    return !memory[key];
  });

  if (needsLLM.length === 0) {
    return { ok: true, answers: {}, error: null, source: 'none_needed', fieldsCount: 0, reviewedBy: 'none' };
  }

  const apiKey = await getApiKey();

  // Verify proxy is reachable before spending time on the call
  if (!apiKey) {
    try {
      const ping = await fetch('http://localhost:3747/health', { signal: AbortSignal.timeout(2000) });
      if (!ping.ok) throw new Error(`Proxy returned HTTP ${ping.status}`);
    } catch(pingErr) {
      return { ok: false, answers: null, error: `Proxy unreachable: ${pingErr.message} — run start_proxy.bat`, source: 'proxy_down', fieldsCount: needsLLM.length, reviewedBy: 'none' };
    }
  }

  const result = await getLLMAnswers(apiKey, needsLLM, jobCtx, profile);
  result.source = apiKey ? 'direct_api' : 'proxy';
  return result;
}

// ─── Gmail proxy ──────────────────────────────────────────────────────────────

async function gmailSearch(query) {
  const { gmailToken } = await chrome.storage.local.get('gmailToken');
  if (!gmailToken) return null;

  try {
    const listResp = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/messages?q=${encodeURIComponent(query)}&maxResults=1`,
      { headers: { Authorization: `Bearer ${gmailToken}` } }
    );
    const listData = await listResp.json();
    if (!listData.messages?.length) return null;

    const msgResp = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/messages/${listData.messages[0].id}?format=full`,
      { headers: { Authorization: `Bearer ${gmailToken}` } }
    );
    return await msgResp.json();
  } catch(e) {
    console.error('JobberAI: Gmail search error:', e);
    return null;
  }
}

// ─── Cover letter generator ───────────────────────────────────────────────────

async function generateCoverLetter(jobCtx) {
  const apiKey = await getApiKey();
  const { profile = {} } = await chrome.storage.local.get('profile');

  const workHighlights = (profile.work_history || []).slice(0, 4)
    .map(w => `- ${w.title} at ${w.company}: ${w.description}`).join('\n');

  const eduLine = profile.education_degree && profile.education_school
    ? `${profile.education_degree} from ${profile.education_school} (${profile.graduation_year || ''})`
    : '';

  const systemPrompt = `You write cover letters for a recent grad from India applying to US tech jobs.
Your writing style rules — follow every one without exception:
1. Sound like a real person, not a template. Slightly imperfect grammar is fine and preferred.
2. NEVER use these words or phrases: passionate, excited, leverage, delve, thrilled, honored, I am pleased, I am eager, dynamic, synergy, spearhead, cutting-edge, world-class, utilize, facilitate, robust, transformative, impactful, seamlessly, empower, humbly.
3. NO em dashes (—) anywhere. Use commas or short sentences instead.
4. NO bullet points. Flowing paragraphs only.
5. Do not open with "I am" or "I would like to". Open with something specific about the role or the candidate's work.
6. Keep it under 180 words total across 3 short paragraphs.
7. Pick 1-2 specific projects or experiences from the candidate's background that directly match what the JD asks for. Name them explicitly.
8. Closing paragraph: one sentence saying you would like to discuss further. No flowery language.`;

  const userPrompt = `Write a cover letter for this job application.

Company: ${jobCtx.company}
Role: ${jobCtx.title}

Candidate background:
- Name: ${profile.full_name || 'Pranav Pradhan'}
- Education: ${eduLine}
- Experience: ${profile.years_experience || '4'} years
${workHighlights}

Job description (use this to pick the most relevant experience to highlight):
${(jobCtx.jd_text || '').slice(0, 2000)}

Write 3 short paragraphs. Pick experiences from above that match what this JD is asking for. Be specific with project names and numbers. Sound human.`;

  try {
    let letter = await callClaude(apiKey, systemPrompt, userPrompt, 600);
    // Post-process: strip any em dashes that slipped through
    letter = letter.replace(/—/g, ',').replace(/--/g, ',');
    return letter;
  } catch(e) {
    return null;
  }
}

// ─── ATS keyword checker ──────────────────────────────────────────────────────

function extractKeywords(jdText) {
  const TECH_TERMS = /\b(python|java|javascript|typescript|react|node|sql|nosql|aws|gcp|azure|docker|kubernetes|k8s|pytorch|tensorflow|jax|spark|kafka|redis|postgres|mongodb|graphql|rest|api|llm|gpt|bert|transformer|ci\/cd|git|linux|agile|scrum)\b/gi;
  const matches = jdText.match(TECH_TERMS) || [];
  return [...new Set(matches.map(m => m.toLowerCase()))];
}

function checkATSKeywords(jdText, profile) {
  const required = extractKeywords(jdText);
  const profileText = JSON.stringify(profile).toLowerCase();
  return required.map(kw => ({
    keyword: kw,
    present: profileText.includes(kw),
  }));
}

// ─── Job tracker ──────────────────────────────────────────────────────────────

async function logApplication(jobCtx, timestamp) {
  const { applications = [] } = await chrome.storage.local.get('applications');
  applications.push({
    company: jobCtx.company,
    title: jobCtx.title,
    url: jobCtx.url,
    applied_at: timestamp,
    status: 'applied',
  });
  // Keep last 500
  if (applications.length > 500) applications.splice(0, applications.length - 500);
  await chrome.storage.local.set({ applications });
}

// ─── Message router ───────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  switch (msg.type) {

    case 'GET_ANSWERS':
      getAnswers(msg.fields, msg.jobCtx)
        .then(result => sendResponse(result))
        .catch(err => sendResponse({ ok: false, answers: null, error: err.message, source: 'exception', fieldsCount: 0, reviewedBy: 'none' }));
      return true;

    case 'GMAIL_SEARCH':
      gmailSearch(msg.query)
        .then(thread => sendResponse(thread))
        .catch(() => sendResponse(null));
      return true;

    case 'OPEN_VERIFY_LINK':
      chrome.tabs.create({ url: msg.link, active: false }, tab => {
        // Close after 3s
        setTimeout(() => chrome.tabs.remove(tab.id), 3000);
        sendResponse({ ok: true });
      });
      return true;

    case 'SAVE_PORTAL_ACCOUNT':
      chrome.storage.local.get('portal_accounts').then(({ portal_accounts = {} }) => {
        portal_accounts[msg.domain] = { email: msg.email, password: msg.password };
        return chrome.storage.local.set({ portal_accounts });
      }).then(() => sendResponse({ ok: true }));
      return true;

    case 'OPEN_SIDEPANEL':
      chrome.sidePanel.open({ tabId: sender.tab?.id }).catch(() => {});
      // Store fill data for sidepanel to pick up
      chrome.storage.local.set({ lastFillResult: msg.fillResult, lastJobCtx: msg.jobCtx });
      sendResponse({ ok: true });
      return true;

    case 'FILL_COMPLETE':
      // Learn from ALL filled fields where label is non-standard (RAG memory growth)
      chrome.storage.local.get(['memory']).then(({ memory = {} }) => {
        const today = new Date().toISOString().slice(0, 10);
        for (const f of (msg.fillResult?.fields || [])) {
          if (!f.label || !f.value) continue;
          if (/prefer not|decline|not wish to answer/i.test(String(f.value))) continue;
          const key = normalizeLabel(f.label);
          if (!key || STATIC_PROFILE_KEYS.has(key)) continue;
          if (!memory[key]) {
            memory[key] = { answer: f.value, label: f.label, source: f.source || 'profile', seen: 1, last_seen: today };
          } else {
            memory[key].seen++;
            memory[key].last_seen = today;
            if (f.source === 'claude' || f.source === 'user_edit') memory[key].answer = f.value;
          }
        }
        chrome.storage.local.set({ memory });
      });
      chrome.storage.local.set({ lastFillResult: msg.fillResult, lastJobCtx: msg.jobCtx, lastClaudeStatus: msg.claudeStatus || null });
      chrome.sidePanel.open({ tabId: sender.tab?.id }).catch(() => {});
      sendResponse({ ok: true });
      return true;

    case 'LOG_APPLICATION':
      logApplication(msg.jobCtx, msg.timestamp || new Date().toISOString())
        .then(() => sendResponse({ ok: true }));
      return true;

    case 'GENERATE_COVER_LETTER':
      generateCoverLetter(msg.jobCtx)
        .then(text => sendResponse({ text }))
        .catch(() => sendResponse({ text: null }));
      return true;

    case 'CHECK_ATS':
      chrome.storage.local.get('profile').then(({ profile = {} }) => {
        const keywords = checkATSKeywords(msg.jdText, profile);
        sendResponse({ keywords });
      });
      return true;

    case 'GET_RESUME_B64':
      chrome.storage.local.get('resume_b64').then(({ resume_b64 }) => {
        sendResponse({ b64: resume_b64 || null });
      });
      return true;

    case 'CLEAR_RATE_LIMIT':
      clearRateLimited().then(() => sendResponse({ ok: true }));
      return true;

    case 'SAVE_MEMORY_EDIT':
      // User edited an answer in sidepanel — save with high confidence
      chrome.storage.local.get('memory').then(({ memory = {} }) => {
        memory[msg.key] = {
          answer: msg.answer,
          label: msg.label,
          source: 'user_edit',
          seen: (memory[msg.key]?.seen || 0) + 1,
          last_seen: new Date().toISOString().slice(0, 10),
        };
        return chrome.storage.local.set({ memory });
      }).then(() => sendResponse({ ok: true }));
      return true;

    default:
      return false;
  }
});

// ─── Long-lived port for Claude calls ────────────────────────────────────────
// sendMessage has a ~5 min SW lifetime; long-lived ports keep the SW alive
// for the full duration of the Claude API call (60-90s for Haiku on large batches).

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'claude_request') return;

  // Chrome MV3 kills service workers if no Chrome API call is in-flight for ~30s.
  // fetch() alone doesn't count. Touch chrome.storage every 20s to keep the SW alive.
  let _keepAliveTimer = null;
  function startKeepAlive() {
    _keepAliveTimer = setInterval(() => chrome.storage.local.get('_sw_ping'), 20000);
  }
  function stopKeepAlive() {
    if (_keepAliveTimer) { clearInterval(_keepAliveTimer); _keepAliveTimer = null; }
  }
  port.onDisconnect.addListener(stopKeepAlive);

  port.onMessage.addListener(async (msg) => {
    if (msg.type !== 'GET_ANSWERS_VIA_PORT') return;
    startKeepAlive();
    try {
      const result = await getAnswers(msg.fields, msg.jobCtx);
      port.postMessage({ type: 'GET_ANSWERS_RESULT', result });
    } catch(err) {
      port.postMessage({ type: 'GET_ANSWERS_RESULT', result: {
        ok: false, answers: {}, error: err.message, source: 'exception', fieldsCount: 0, reviewedBy: 'none',
      }});
    } finally {
      stopKeepAlive();
    }
  });
});

// On install or update — reload profile from data/profile.json
// Merges into existing storage so user edits to non-profile keys (memory, accounts) are preserved
chrome.runtime.onInstalled.addListener(async (details) => {
  try {
    const url = chrome.runtime.getURL('data/profile.json');
    const resp = await fetch(url);
    const freshProfile = await resp.json();
    // Merge: fresh profile fields win, but preserve any user-added keys not in profile.json
    const { profile: existing = {} } = await chrome.storage.local.get('profile');
    const merged = { ...existing, ...freshProfile };
    await chrome.storage.local.set({ profile: merged });
    console.log('JobberAI: profile loaded (' + details.reason + ')');
  } catch(e) {
    console.error('JobberAI: failed to load profile.json', e);
  }

  // Clean up stale EEO cache entries
  // Purge: wrong gender ("Woman"/"Female"), stale ethnicity from UUID survey keys ("Asian", "Any other…")
  try {
    const { memory = {} } = await chrome.storage.local.get('memory');
    let purged = 0;
    for (const [key, entry] of Object.entries(memory)) {
      const ans = String(entry.answer || '');
      const isUUIDSurveyKey = /surveys.*responses.*field/i.test(key);
      // UUID Lever cards field keys — never reusable, purge unconditionally
      const isUUIDCardsKey = /cards[0-9a-f]{8,}field\d+/i.test(key);
      // Section-heading keys that bleed into text field fills (e.g. "referral_details" → "No")
      const isSectionHeadingKey = /^referral_details?$|^work_authorization_status$|^referral_info$|^referred_by_a_|^referred_by_current/i.test(key);
      const isBadEEO = /\bwoman\b|\bfemale\b/i.test(ans);
      const isStaleEthnicity = isUUIDSurveyKey && /\basian\b|any other|bangladeshi|indian|hispanic|black/i.test(ans);
      if (isUUIDCardsKey || isSectionHeadingKey || isBadEEO || isStaleEthnicity) {
        delete memory[key];
        purged++;
      }
    }
    if (purged > 0) {
      await chrome.storage.local.set({ memory });
      console.log(`JobberAI: purged ${purged} stale EEO cache entries`);
    }
  } catch(e) {
    console.error('JobberAI: memory cleanup failed', e);
  }
});
