// Autofill engine — ported from fast_autofill.py + screener_engine.py
// Three-tier resolution: alias → regex bank → memory → Claude

const LABEL_ALIASES = {
  "full name":                "full_name",
  "name":                     "full_name",
  "your name":                "full_name",
  "first name":               "first_name",
  "firstname":                "first_name",
  "given name":               "first_name",
  "last name":                "last_name",
  "lastname":                 "last_name",
  "family name":              "last_name",
  "surname":                  "last_name",
  "e-mail":                   "email",
  "email address":            "email",
  "your email":               "email",
  "email id":                 "email",
  "enter your email":         "email",
  "enter email":              "email",
  "mobile":                   "phone",
  "mobile number":            "phone",
  "telephone":                "phone",
  "cell":                     "phone",
  "cell phone":               "phone",
  "phone number":             "phone",
  "phone":                    "phone",
  "contact number":           "phone",
  "phone country code":       "phone_country_code",
  "country code":             "phone_country_code",
  "linkedin profile":         "linkedin_url",
  "linkedin profile url":     "linkedin_url",
  "linkedin url":             "linkedin_url",
  "linkedin":                 "linkedin_url",
  "github profile":           "github_url",
  "github url":               "github_url",
  "github":                   "github_url",
  "website":                  "portfolio_url",
  "personal website":         "portfolio_url",
  "portfolio":                "portfolio_url",
  "portfolio url":            "portfolio_url",
  "city":                     "address_city",
  "state":                    "address_state",
  "state/province":           "address_state",
  "zip":                      "address_zip",
  "zip code":                 "address_zip",
  "postal code":              "address_zip",
  "postcode":                 "address_zip",
  "country":                  "address_country",
  "street address":           "address_line1",
  "address":                  "address_line1",
  "address line 1":           "address_line1",
  "address 1":                "address_line1",
  "phone extension":          null,
  "legal name":               "full_name",
  "legal full name":          "full_name",
  "preferred name":           "first_name",
  "preferred first name":     "first_name",
  "desired salary":           "salary_expectation",
  "expected salary":          "salary_expectation",
  "salary expectation":       "salary_expectation",
  "compensation":             "salary_expectation",
  "salary":                   "salary_expectation",
  "years of experience":      "years_experience",
  "years experience":         "years_experience",
  "how many years":           "years_experience",
  "years of relevant":        "years_experience",
  "years of professional":    "years_experience",
  "total years":              "years_experience",
  "current ctc":              "salary_expectation",
  "expected ctc":             "salary_expectation",
  "notice period":            null,
  "work authorization":       "work_authorization",
  "authorized to work":       "work_authorization",
  "graduation year":          "graduation_year",
  "degree":                   "education_degree",
  "highest degree":           "education_degree",
  "highest education":        "education_degree",
  "cover letter":             "cover_letter_default",
  "message":                  "cover_letter_default",
  "why are you interested":   "cover_letter_default",
  "why this company":         "cover_letter_default",
  "why this role":            "cover_letter_default",
  "where are you currently located":  "location",
  "where are you located":            "location",
  "current location":                 "location",
  "your location":                    "location",
  "city, state":                      "location",
  "city or state":                    "location",
  "skills":                   "skills",
  "technical skills":         "skills",
  "key skills":               "skills",
  "technologies":             "skills",
  "tools":                    "skills",
  "tools & technologies":     "skills",
  "programming languages":    "skills_by_category.languages",
  "languages":                "skills_by_category.languages",
  "frameworks":               "skills_by_category.ml_frameworks",
  "libraries":                "skills_by_category.ml_frameworks",
  "ml frameworks":            "skills_by_category.ml_frameworks",
  "cloud platforms":          "skills_by_category.cloud_devops",
  "cloud":                    "skills_by_category.cloud_devops",
  "devops tools":             "skills_by_category.cloud_devops",
  "current company":          "current_company",
  "current employer":         "current_company",
  "employer":                 "current_company",
  "company":                  "current_company",
  "organization":             "current_company",
  "bio":                      "bio",
  "about you":                "bio",
  "about yourself":           "bio",
  "professional summary":     "bio",
  "summary":                  "bio",
  "headline":                 "bio",
  "notice period":            "notice_period",
  "available start date":     "start_date",
  "start date":               "start_date",
  "strength":                 "strength",
  "greatest strength":        "strength",
  "address line 2":           null,
  "apartment":                null,
  "suite":                    null,
};

// Profile key lookup map (maps address_* to profile flat keys)
const PROFILE_KEY_MAP = {
  "address_city":    "city",
  "address_state":   "state",
  "address_zip":     "zip_code",
  "address_country": "country",
  "address_line1":   "address",
  "location":        "location",
};

const QUESTION_BANK = [
  { re: /authorized.{0,30}work|right.{0,10}work|eligible.{0,15}work|legally.{0,15}work|lawfully/i, key: "work_authorization" },
  { re: /sponsor|visa.{0,20}sponsor|require.{0,10}sponsor|need.{0,10}sponsor/i,                     key: "requires_sponsorship" },
  { re: /us.{0,5}citizen|permanent.{0,5}resid|green.{0,5}card/i,                                    key: "us_citizen_or_pr" },
  { re: /salary|compensation|expected.{0,15}pay|desired.{0,10}pay|pay.{0,15}expect/i,               key: "salary_expectation" },
  { re: /years?.{0,10}experience|how.{0,15}years|experience.{0,10}years/i,                          key: "years_experience" },
  { re: /start.{0,10}date|available.{0,15}start|when.{0,15}start|earliest.{0,10}start/i,            key: "_start_date" },
  { re: /background.{0,10}check|consent.{0,20}background|drug.{0,10}test/i,                         key: "_yes" },
  { re: /remote|work.{0,15}remote|preference.{0,15}remote|remote.{0,15}work/i,                      key: "remote_preference" },
  { re: /reloc|willing.{0,15}reloc|open.{0,10}reloc|relocate/i,                                     key: "willing_to_relocate" },
  { re: /highest.{0,20}educ|education.{0,15}level|highest.{0,10}degree/i,                           key: "education_degree" },
  { re: /grad.{0,10}year|when.{0,15}graduat|expected.{0,15}grad|graduation/i,                       key: "graduation_year" },
  { re: /driver.{0,10}licen|driving.{0,10}licen|valid.{0,10}licen/i,                                key: "_yes" },
  { re: /18.{0,10}(years|or older)|over.{0,5}18|legal.{0,10}age|at least 18|18\+|over the age of 18/i, key: "_yes" },

  // Pronouns / gender
  { re: /pronoun/i,                                                                                   key: "_pronouns" },

  // Today's date
  { re: /today.{0,10}date|current.{0,10}date|date.{0,10}today|date.{0,5}sign/i,                   key: "_today" },
  { re: /^date$|application.{0,10}date|submission.{0,10}date/i,                                    key: "_today" },

  // Common yes/no compliance questions
  { re: /relativ|family.{0,15}member|friend.{0,15}(work|employ)|know.{0,15}anyone.{0,15}(work|employ)/i, key: "_no" },
  { re: /spouse|domestic.{0,10}partner|significant.{0,10}other|partner.{0,10}(work|employ)/i,     key: "_no" },
  { re: /armed.{0,10}force|military.{0,10}(member|service|family)|active.{0,10}duty|serving.{0,10}military/i, key: "_no" },
  { re: /family.{0,15}(member|relative).{0,20}(armed|military|service)/i,                         key: "_no" },
  { re: /refer.{0,15}(by|from)|referred.{0,10}by|who referred/i,                                   key: "_no" },
  { re: /non.?compete|non.?disclosure|nda.{0,20}(current|previous|prior)/i,                        key: "_no" },
  { re: /felony|criminal.{0,20}(record|conviction|charge)|convicted|been arrested/i,                key: "_no" },
  { re: /terminat.{0,20}(for cause|involuntary)|fired.{0,15}for.{0,15}cause/i,                    key: "_no" },
  { re: /conflict.{0,15}interest/i,                                                                  key: "_no" },
  { re: /currently.{0,15}(active|hold).{0,15}security.{0,10}clearance/i,                           key: "_no" },
  { re: /security.{0,10}clearance/i,                                                                 key: "_no" },
  { re: /previously.{0,15}(apply|applied|work|employ).{0,20}(here|this company|with us|by us)/i,   key: "_no" },
  { re: /worked.{0,20}(here|this company|for us) before/i,                                         key: "_no" },
  { re: /previously.{0,20}employed|formerly.{0,20}employed|ever.{0,20}employed.{0,20}by/i,         key: "_no" },
  { re: /contract.{0,10}(prevent|restrict|prohibit)|restrictive.{0,10}covenant/i,                  key: "_no" },
  { re: /currently.{0,15}employ|are you.{0,15}(currently.{0,5})?employ/i,                         key: "_no" },
  { re: /on.?call|available.{0,10}on.?call/i,                                                       key: "_yes" },
  { re: /overtime|willing.{0,10}(to work )?overtime/i,                                              key: "_yes" },
  { re: /willing.{0,15}travel|open.{0,10}travel|travel.{0,10}required/i,                           key: "_yes" },
  { re: /drug.{0,10}test|substance.{0,10}test|consent.{0,10}drug/i,                               key: "_yes" },
  { re: /background.{0,10}check|consent.{0,20}background/i,                                        key: "_yes" },
  { re: /agree.{0,20}terms|accept.{0,20}terms|terms.{0,10}(and.{0,5})?condition/i,                key: "_yes" },
  { re: /essential.{0,20}function|perform.{0,20}(essential|job).{0,20}(function|dut)|with.{0,20}reasonable.{0,20}accommodation/i, key: "_yes" },
  { re: /meet.{0,20}(preferred|minimum|required).{0,20}qualif|qualif.{0,20}(for this|meet)/i,    key: "_yes" },
  { re: /minimum.{0,20}qualif|basic.{0,20}qualif|required.{0,20}qualif/i,                        key: "_yes" },
  { re: /how did you (hear|find|learn).{0,20}(about|of)|source of (application|referral)|where did you hear/i, key: "_heard" },
  // Office attendance / hybrid questions
  { re: /work.{0,20}(from.{0,10}office|in.{0,10}office|on.?site)|office.{0,15}(days|hours|week)|days?.{0,10}(per|a).{0,5}week.{0,20}(office|in.person|on.site)/i, key: "_yes" },
  { re: /commut|onsite|in.person.{0,20}(require|expect|able)/i,                                    key: "_yes" },
  // Acknowledgment / legal checkboxes
  { re: /arbitration|i acknowledge|i confirm.{0,15}(read|above)|certify.{0,20}that|i have read.{0,20}(and|above)/i, key: "_yes" },
  { re: /agree.{0,20}(policy|above|statement)|accept.{0,20}(agreement|policy)/i,                   key: "_yes" },
  // Additional information / motivation → Claude
  { re: /additional.{0,20}information|motivation.{0,15}apply|additional.{0,15}context|anything.{0,20}(else|know)|is there.{0,20}anything/i, key: "_claude" },
  { re: /^phone$|phone.{0,10}number|mobile.{0,10}number|telephone/i,                                key: "phone" },
  { re: /linkedin|linkedin.{0,10}url|linkedin.{0,10}profile/i,                                      key: "linkedin_url" },
  { re: /github|github.{0,10}url|github.{0,10}profile/i,                                            key: "github_url" },
  { re: /portfolio|personal.{0,10}website|website.{0,10}url/i,                                      key: "portfolio_url" },
  { re: /cover.{0,10}letter|additional.{0,15}comment|anything.{0,20}add/i,                          key: "cover_letter_default" },
  // Demographic survey / location selects
  { re: /what.{0,10}(is|is your).{0,10}location|demographic.{0,15}location|survey.{0,10}location/i, key: "address_state" },
  // Consent / marketing checkboxes
  { re: /consent.{0,20}(contact|reach|email|newsletter)|may.{0,15}contact.{0,15}(me|you)|future.{0,20}(job|opportun|position)/i, key: "_yes" },
  { re: /^gender$|gender.{0,10}identity/i,                                                           key: "_gender" },
  { re: /^race$|^ethnicity$|ethnic.{0,15}group|racial/i,                                            key: "eeo_ethnicity" },
  { re: /veteran|military.{0,15}status/i,                                                            key: "_no_veteran" },
  { re: /disabilit/i,                                                                                 key: "_no_disability" },
  // Open-ended → Claude
  { re: /why.{0,20}(us|company|organization|firm)|interest.{0,20}(us|company)/i,                    key: "_claude" },
  { re: /why.{0,20}(role|position|job|this.{0,10}opport)/i,                                         key: "_claude" },
  { re: /strength|what.{0,20}bring|superpower|best.{0,10}qualit/i,                                  key: "_claude" },
  { re: /biggest.{0,20}project|notable.{0,20}project|proud.{0,20}project/i,                         key: "_claude" },
  { re: /challenge|difficult.{0,20}(situation|problem)|obstacle|overcome/i,                          key: "_claude" },
  { re: /tell.{0,20}(us|me).{0,20}(yourself|background)|introduce|brief.{0,10}bio/i,               key: "_claude" },
  { re: /relevant.{0,20}experience|describe.{0,20}experience/i,                                      key: "_claude" },
];

function _todayFormatted() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const yyyy = d.getFullYear();
  return `${mm}/${dd}/${yyyy}`;  // MM/DD/YYYY — most common on US forms
}

const STATIC_SYNTHETIC = {
  "_yes":        "Yes",
  "_no":         "No",
  "_start_date": "2 weeks",
  "_skip":       null,
  "_heard":      "Company career website",
  "_today":      _todayFormatted(),
  "_pronouns":   "He/Him",
  "_gender":     "Male",
  "_no_disability": "No, I do not have a disability",
  "_no_veteran":    "I am not a protected veteran",
};

function fuzzyMatchOption(value, options) {
  if (!options || !options.length) return null;
  const target = value.toLowerCase().trim();
  let m = options.find(o => o.toLowerCase().trim() === target);
  if (m) return m;
  m = options.find(o => o.toLowerCase().includes(target) || target.includes(o.toLowerCase().trim()));
  return m || null;
}

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[^\w\s]/g, '')   // strip ALL non-word, non-space chars (handles ✱ ❋ ＊ etc.)
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .trim();
}

function resolveAnswer(field, profile, memory) {
  const label = field.label_text.toLowerCase().trim();
  const stripped = label.replace(/[^\w\s]/g, '').replace(/\s+/g, ' ').trim();

  // Tier 1: alias table
  for (const probe of [label, stripped]) {
    if (!(probe in LABEL_ALIASES)) continue;
    const profileKey = LABEL_ALIASES[probe];
    if (profileKey === null) return null; // explicitly skip
    // Support nested keys like "skills_by_category.languages"
    if (profileKey.includes('.')) {
      const [parent, child] = profileKey.split('.');
      return profile[parent]?.[child] ?? null;
    }
    const mappedKey = PROFILE_KEY_MAP[profileKey] || profileKey;
    return profile[mappedKey] ?? profile[profileKey] ?? null;
  }

  // Tier 2: QUESTION_BANK regex — test both raw label AND stripped (handles "Phone *" → "phone")
  for (const { re, key } of QUESTION_BANK) {
    if (re.test(label) || re.test(stripped) || re.test(field.placeholder)) {
      if (key === '_claude') return '_needs_claude';
      if (key in STATIC_SYNTHETIC) {
        const synth = STATIC_SYNTHETIC[key];
        if (synth === null) return null; // _skip
        // For select/radio — map Yes/No to the closest available option
        if ((key === '_yes' || key === '_no') &&
            (field.select_options?.length || field.radio_options?.length)) {
          const opts = field.select_options?.length ? field.select_options : field.radio_options;
          return fuzzyMatchOption(synth, opts) ?? synth;
        }
        return synth;
      }
      const mappedKey = PROFILE_KEY_MAP[key] || key;
      const val = profile[mappedKey] ?? profile[key];
      return val !== undefined ? String(val) : null;
    }
  }

  // Tier 3: memory lookup — exact then fuzzy token overlap
  const memKey = normalizeLabel(label);
  if (memory[memKey]) return memory[memKey].answer;
  const fuzzyHit = fuzzyMemoryLookup(label, memory);
  if (fuzzyHit) return fuzzyHit.answer;

  // Tier 4: field-type fallback
  if (field.field_type === 'email') return profile.email || null;
  if (field.field_type === 'tel') return profile.phone || null;
  if (field.field_type === 'url') return profile.linkedin_url || profile.github_url || null;

  return '_needs_claude';
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function isTruthy(value) {
  return /^(yes|true|1|on|checked)$/i.test(String(value).trim());
}

function fillSelect(el, value) {
  const target = value.toLowerCase().trim();
  const opts = Array.from(el.options);
  // exact match first
  let opt = opts.find(o => o.text.toLowerCase().trim() === target);
  // partial match
  if (!opt) opt = opts.find(o => o.text.toLowerCase().includes(target) || target.includes(o.text.toLowerCase()));
  // value attribute match
  if (!opt) opt = opts.find(o => o.value.toLowerCase() === target);
  if (opt) {
    el.value = opt.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}

function _clickRadioInput(r) {
  // Prefer clicking the associated label (works when input is visually hidden — Ashby, Lever, etc.)
  if (r.id) {
    const lbl = document.querySelector(`label[for="${r.id}"]`);
    if (lbl) { lbl.click(); return; }
  }
  const parentLbl = r.closest('label');
  if (parentLbl) { parentLbl.click(); return; }
  // Fallback: directly check + fire events
  r.checked = true;
  r.dispatchEvent(new Event('click',  { bubbles: true }));
  r.dispatchEvent(new Event('change', { bubbles: true }));
}

function _getRadioLabel(r, domRoot) {
  // 1. label[for=id]
  if (r.id) {
    const lbl = (domRoot || document).querySelector(`label[for="${r.id}"]`);
    if (lbl) return (lbl.innerText || lbl.textContent || '').toLowerCase().trim();
  }
  // 2. aria-label
  const al = r.getAttribute('aria-label');
  if (al) return al.toLowerCase().trim();
  // 3. parent label
  const pl = r.closest('label');
  if (pl) return (pl.innerText || pl.textContent || '').toLowerCase().trim();
  // 4. value attribute
  return (r.value || '').toLowerCase().trim();
}

function fillRadio(field, value) {
  const target = value.toLowerCase().trim();
  const radios = document.querySelectorAll(`input[type="radio"][name="${field.group_name}"]`);
  for (const r of radios) {
    const lblText = _getRadioLabel(r);
    if (lblText === target || lblText.includes(target) || r.value.toLowerCase() === target) {
      _clickRadioInput(r);
      return;
    }
  }
  // fallback: yes-like → first radio, no-like → last radio
  if (radios.length > 0) {
    if (isTruthy(value))  { _clickRadioInput(radios[0]); return; }
    if (/^no$/i.test(value.trim())) { _clickRadioInput(radios[radios.length - 1]); return; }
  }
}

function fillAriaRadio(field, value) {
  const target = value.toLowerCase().trim();
  // Find the radiogroup by group_name label in the DOM
  const groups = document.querySelectorAll('[role="radiogroup"]');
  for (const rg of groups) {
    const rgLabel = (rg.getAttribute('aria-label') || rg.innerText || '').toLowerCase();
    if (!rgLabel.includes(field.group_name.toLowerCase().slice(0, 15))) continue;
    const options = rg.querySelectorAll('[role="radio"]');
    for (const opt of options) {
      const optText = (opt.getAttribute('aria-label') || opt.innerText || opt.textContent || '').toLowerCase().trim();
      if (optText === target || optText.includes(target) || target.includes(optText)) {
        opt.click();
        opt.dispatchEvent(new Event('change', { bubbles: true }));
        return;
      }
    }
    // Fallback: yes/truthy → first, no → last
    if (options.length > 0) {
      if (isTruthy(value)) { options[0].click(); return; }
      if (/^no$/i.test(value.trim())) { options[options.length - 1].click(); return; }
    }
  }
}

async function uploadFile(el) {
  // Resume upload is handled via service_worker message (DataTransfer API)
  // Sending a message to content.js which knows the resume blob
  const result = await chrome.runtime.sendMessage({ type: 'GET_RESUME_B64' });
  if (!result || !result.b64) return;

  const byteChars = atob(result.b64);
  const bytes = new Uint8Array(byteChars.length);
  for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i);
  const blob = new Blob([bytes], { type: 'application/pdf' });
  const file = new File([blob], 'Pranav_Pradhan_resume.pdf', { type: 'application/pdf' });
  const dt = new DataTransfer();
  dt.items.add(file);
  el.files = dt.files;
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

async function _fillText(el, value) {
  el.focus();
  // React native setter trick — signals React that value changed
  const proto = el instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc?.set) {
    desc.set.call(el, '');
    desc.set.call(el, value);
  } else {
    el.value = value;
  }
  el.dispatchEvent(new InputEvent('input',  { bubbles: true, composed: true, data: value }));
  el.dispatchEvent(new Event('change',       { bubbles: true }));

  // If React didn't accept the value (controlled component with custom handler),
  // fall back to execCommand — selects all then inserts text as if typed
  if (el.value !== value) {
    el.select?.();
    try {
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, value);
    } catch(e) {}
    // Still didn't stick? Fire key events one char at a time (last resort)
    if (el.value !== value) {
      if (desc?.set) desc.set.call(el, '');
      else el.value = '';
      el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
      for (const char of value) {
        el.dispatchEvent(new KeyboardEvent('keydown',  { key: char, bubbles: true }));
        el.dispatchEvent(new KeyboardEvent('keypress', { key: char, bubbles: true }));
        if (desc?.set) desc.set.call(el, el.value + char);
        else el.value += char;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, data: char }));
        el.dispatchEvent(new KeyboardEvent('keyup', { key: char, bubbles: true }));
      }
    }
  }
  el.dispatchEvent(new Event('blur', { bubbles: true }));
}

async function fillField(field, value) {
  let el = document.querySelector(field.selector);
  if (!el) {
    // XPath fallback
    try {
      const xr = document.evaluate(
        '//*[@id="' + field.id + '"]', document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null
      );
      el = xr.singleNodeValue;
    } catch(e) {}
  }
  if (!el) return false;

  switch (field.field_type) {
    case 'text':
    case 'email':
    case 'tel':
    case 'url':
    case 'number':
    case 'search':
    case 'textarea': {
      await _fillText(el, value);
      break;
    }
    case 'select':
      fillSelect(el, value);
      break;
    case 'radio':
      fillRadio(field, value);
      break;
    case 'aria_radio':
      fillAriaRadio(field, value);
      break;
    case 'checkbox':
      el.checked = isTruthy(value);
      el.dispatchEvent(new Event('change', { bubbles: true }));
      break;
    case 'file':
      await uploadFile(el);
      break;
  }
  await sleep(100 + Math.random() * 80);
  return true;
}

async function autofillPage(profile, memory, claudeAnswers) {
  const fields = detectFormFields();
  const needsClaude = [];
  const results = { filled: 0, skipped: 0, claude: 0, fields: [] };

  for (const f of fields) {
    if (f.current_value && f.current_value.length > 2 &&
        f.field_type !== 'radio' && f.field_type !== 'checkbox') {
      results.skipped++;
      continue;
    }

    const ans = resolveAnswer(f, profile, memory);
    if (ans === '_needs_claude') {
      needsClaude.push(f);
      continue;
    }
    if (ans === null) {
      results.skipped++;
      continue;
    }
    const ok = await fillField(f, ans);
    if (ok) {
      results.filled++;
      results.fields.push({ label: f.label_text, value: ans, source: 'profile' });
    }
  }

  // Fill Claude-answered fields
  if (claudeAnswers) {
    for (const f of needsClaude) {
      const memKey = normalizeLabel(f.label_text);
      const ans = claudeAnswers[memKey] || claudeAnswers[f.label_text];
      if (ans) {
        const ok = await fillField(f, ans);
        if (ok) {
          results.claude++;
          results.fields.push({ label: f.label_text, value: ans, source: 'claude' });
        }
      }
    }
  }

  // Direct portal fill — catches fields form_detector missed + React-cleared values
  const directCount = await directPortalFill(profile);
  if (directCount > 0) results.filled += directCount;

  results.needsClaude = needsClaude;
  return results;
}

function buildFallbackAnswers(fields, profile, memory) {
  const answers = {};
  for (const f of fields) {
    const ans = resolveAnswer(f, profile, memory);
    if (ans && ans !== '_needs_claude') {
      answers[normalizeLabel(f.label_text)] = ans;
    }
  }
  return answers;
}

// ─── Direct portal fill ───────────────────────────────────────────────────────
// Fallback for fields that form_detector may have missed or that React cleared.
// Runs after the main fill loop. Tries well-known input[name] / input[type] selectors.

const DIRECT_FILL_MAP = [
  // Full name variants
  { selectors: ['input[name="name"]', 'input[name="full_name"]', 'input[name="fullName"]', 'input[autocomplete="name"]'], key: 'full_name' },
  // First / last
  { selectors: ['input[name="first_name"]', 'input[name="firstName"]', 'input[autocomplete="given-name"]'], key: 'first_name' },
  { selectors: ['input[name="last_name"]', 'input[name="lastName"]', 'input[autocomplete="family-name"]'], key: 'last_name' },
  // Email
  { selectors: ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]'], key: 'email' },
  // Phone
  { selectors: ['input[type="tel"]', 'input[name="phone"]', 'input[name="phoneNumber"]', 'input[name="phone_number"]'], key: 'phone' },
  // Company
  { selectors: ['input[name="org"]', 'input[name="company"]', 'input[name="current_company"]', 'input[name="currentCompany"]'], key: 'current_company' },
  // LinkedIn / GitHub
  { selectors: ['input[name="linkedin"]', 'input[name="linkedin_url"]', 'input[name="linkedinUrl"]'], key: 'linkedin_url' },
  { selectors: ['input[name="github"]', 'input[name="github_url"]', 'input[name="githubUrl"]'], key: 'github_url' },
  // Portfolio
  { selectors: ['input[name="portfolio"]', 'input[name="portfolio_url"]', 'input[name="website"]'], key: 'portfolio_url' },
];

async function directPortalFill(profile) {
  let count = 0;
  for (const { selectors, key } of DIRECT_FILL_MAP) {
    const value = profile[key];
    if (!value) continue;
    for (const sel of selectors) {
      let el;
      try { el = document.querySelector(sel); } catch(e) { continue; }
      if (!el) continue;
      const t = (el.type || '').toLowerCase();
      if (t === 'hidden' || t === 'password') continue;
      if (el.value && el.value.length > 2) continue; // already filled
      await _fillText(el, value);
      count++;
      break;
    }
  }
  return count;
}

// ─── Fuzzy memory lookup ──────────────────────────────────────────────────────
// When exact normalizeLabel(label) doesn't hit memory, try token overlap.
// Finds the best memory key where ≥80% of label tokens appear in the key or vice-versa.

function fuzzyMemoryLookup(label, memory) {
  const tokens = normalizeLabel(label).split('_').filter(Boolean);
  if (tokens.length === 0) return null;
  let bestScore = 0, bestEntry = null;
  for (const [key, entry] of Object.entries(memory)) {
    const keyTokens = key.split('_').filter(Boolean);
    const overlap = tokens.filter(t => keyTokens.includes(t)).length;
    const score = overlap / Math.max(tokens.length, keyTokens.length);
    if (score >= 0.8 && score > bestScore) {
      bestScore = score;
      bestEntry = entry;
    }
  }
  return bestEntry;
}
