// Universal form field detector — ported from src/browser/form_detector.py (_JS_EXTRACTOR)
// Works on Greenhouse, Lever, Ashby, Workday (Shadow DOM), Lever, SPAs

function detectFormFields() {
  const results = [];
  const seen = new Set();
  const radioGroups = {};

  function safeAttr(el, name) {
    try { return el.getAttribute(name) || ''; } catch(e) { return ''; }
  }

  function getLabel(el, root) {
    // 1. aria-label
    const al = safeAttr(el, 'aria-label');
    if (al) return al;

    // 2. label[for=id]
    if (el.id) {
      const lbl = root.querySelector('label[for="' + el.id + '"]');
      if (lbl) return (lbl.innerText || lbl.textContent || '').replace(/\s+/g, ' ');
    }

    // 3. aria-labelledby
    const lblBy = safeAttr(el, 'aria-labelledby');
    if (lblBy) {
      const lblEl = document.getElementById(lblBy);
      if (lblEl) return (lblEl.innerText || lblEl.textContent || '').replace(/\s+/g, ' ');
    }

    // 4. Closest <label> ancestor
    let parent = el.parentElement;
    let depth = 0;
    while (parent && depth < 5) {
      if (parent.tagName === 'LABEL') {
        // Strip nested input/select/textarea text so country lists don't leak into label
        const clone = parent.cloneNode(true);
        clone.querySelectorAll('input, select, textarea, button').forEach(c => c.remove());
        const t = (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim();
        if (t) return t;
      }
      const childLbl = parent.querySelector(':scope > label');
      if (childLbl) {
        const clone = childLbl.cloneNode(true);
        clone.querySelectorAll('input, select, textarea, button').forEach(c => c.remove());
        const t = (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim();
        if (t) return t;
      }
      parent = parent.parentElement;
      depth++;
    }

    // 5. Fallback: placeholder or name
    return el.placeholder || el.name || '';
  }

  function isVisible(el) {
    const elType = (el.type || '').toLowerCase();
    // Radio, checkbox, and file inputs are routinely visually-hidden behind styled elements.
    // Don't apply bounding-box or opacity checks for them.
    const isToggle = (elType === 'radio' || elType === 'checkbox' || elType === 'file');

    try {
      const style = window.getComputedStyle(el);
      if (style.display === 'none') return false;
      if (style.visibility === 'hidden') return false;
      if (!isToggle) {
        if (parseFloat(style.opacity) < 0.01) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 && rect.height <= 0) return false;
        if (rect.right < -50 || rect.bottom < -50) return false;
      }
    } catch(e) { return false; }

    let p = el.parentElement;
    while (p && p !== document.documentElement) {
      if (p.getAttribute('aria-hidden') === 'true') return false;
      p = p.parentElement;
    }
    return true;
  }

  function buildSelector(el) {
    if (el.id) {
      try { return '#' + CSS.escape(el.id); } catch(e) {}
    }
    const name = safeAttr(el, 'name');
    if (name) {
      const tag = el.tagName.toLowerCase();
      return tag + '[name="' + name.replace(/"/g, '\\"') + '"]';
    }
    const dataField = safeAttr(el, 'data-field');
    if (dataField) return '[data-field="' + dataField.replace(/"/g, '\\"') + '"]';
    const dataAuto = safeAttr(el, 'data-automation-id');
    if (dataAuto) return '[data-automation-id="' + dataAuto.replace(/"/g, '\\"') + '"]';
    const tag = el.tagName.toLowerCase();
    const siblings = Array.from(document.querySelectorAll(tag));
    const idx = siblings.indexOf(el);
    return idx >= 0 ? tag + ':nth-of-type(' + (idx + 1) + ')' : tag;
  }

  function collectElements(root, domRoot) {
    const els = root.querySelectorAll('input, textarea, select');
    for (const el of els) {
      processElement(el, domRoot || root);
    }

    // Detect ARIA radio groups (Lever pronoun picker, Ashby custom selects)
    // role="radiogroup" containers with role="radio" children
    const radioGroups = root.querySelectorAll('[role="radiogroup"]');
    for (const rg of radioGroups) {
      const groupLabel = (rg.getAttribute('aria-label') ||
        rg.getAttribute('aria-labelledby') && document.getElementById(rg.getAttribute('aria-labelledby'))?.innerText ||
        rg.closest('[class*="question"],[class*="field"],[class*="group"]')
          ?.querySelector('label,legend,[class*="label"]')?.innerText || '').trim();
      if (!groupLabel) continue;
      const dedupeKey = 'ariagroup:' + groupLabel;
      if (seen.has(dedupeKey)) continue;
      seen.add(dedupeKey);
      const options = Array.from(rg.querySelectorAll('[role="radio"]'))
        .map(r => (r.getAttribute('aria-label') || r.innerText || r.textContent || '').trim())
        .filter(Boolean);
      if (options.length === 0) continue;
      const firstRadio = rg.querySelector('[role="radio"]');
      results.push({
        selector: firstRadio ? buildSelector(firstRadio) : '[role="radiogroup"]',
        label_text: groupLabel.toLowerCase(),
        field_type: 'aria_radio',
        name: groupLabel,
        id: rg.id || '',
        placeholder: '',
        required: rg.getAttribute('aria-required') === 'true',
        current_value: '',
        radio_options: options,
        select_options: [],
        group_name: groupLabel,
        _rg_el: rg,
      });
    }

    // Recurse into shadow roots (Workday compatibility)
    const all = root.querySelectorAll('*');
    for (const node of all) {
      if (node.shadowRoot) collectElements(node.shadowRoot, domRoot || root);
    }
  }

  function processElement(el, domRoot) {
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();

    if (type === 'hidden') return;
    if (type === 'password') return;
    if (el.disabled) return;
    // Allow tabindex=-1 on radio/checkbox/file (all are routinely hidden behind styled elements)
    if (safeAttr(el, 'tabindex') === '-1' && type !== 'radio' && type !== 'checkbox' && type !== 'file') return;
    // File inputs are always hidden (display:none / opacity:0) behind a styled upload button
    if (type !== 'file' && !isVisible(el)) return;

    const label = (getLabel(el, domRoot) || '').trim().replace(/\s+/g, ' ');
    const dedupeKey = tag + ':' + (el.name || '') + ':' + (el.id || '') + ':' + label;
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);

    if (type === 'radio') {
      const groupName = el.name || el.id || label;
      if (!radioGroups[groupName]) radioGroups[groupName] = { label: label, options: [] };
      const optLabel = safeAttr(el, 'aria-label') ||
                       (el.id && domRoot.querySelector('label[for="' + el.id + '"]')
                          ? (domRoot.querySelector('label[for="' + el.id + '"]').innerText || '').trim()
                          : '') ||
                       el.value || '';
      radioGroups[groupName].options.push({ label: optLabel.trim(), value: el.value });
      return;
    }

    let fieldType = type || tag;
    if (tag === 'textarea') fieldType = 'textarea';
    if (tag === 'select') fieldType = 'select';
    if (!fieldType || fieldType === 'text' || fieldType === '') fieldType = 'text';

    const selectOptions = tag === 'select'
      ? Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean)
      : [];

    const required = el.required || safeAttr(el, 'aria-required') === 'true';

    results.push({
      selector: buildSelector(el),
      label_text: label.toLowerCase(),
      field_type: fieldType,
      name: el.name || '',
      id: el.id || '',
      placeholder: (el.placeholder || '').toLowerCase(),
      required: required,
      current_value: el.value || '',
      radio_options: [],
      select_options: selectOptions,
      group_name: '',
    });
  }

  collectElements(document, document);

  // Emit collapsed radio groups
  for (const [groupName, group] of Object.entries(radioGroups)) {
    if (!group.options.length) continue;
    const firstInput = document.querySelector('input[type="radio"][name="' + groupName + '"]') ||
                       document.querySelector('input[type="radio"][id="' + groupName + '"]');
    const selector = firstInput ? buildSelector(firstInput)
                                : 'input[type="radio"][name="' + groupName + '"]';

    // Prefer the group-level question label over the first option's individual label.
    // Walk up from the first radio to find a legend, or a parent label/heading that
    // is NOT itself one of the radio option labels.
    let groupLabel = '';
    if (firstInput) {
      const optionLabels = new Set(group.options.map(o => (o.label || '').toLowerCase().trim()));
      let p = firstInput.parentElement;
      let depth = 0;
      while (p && depth < 10) {
        // ── 1. Direct child <label> (most specific) ──────────────────────────
        const childLabel = p.querySelector(':scope > label');
        if (childLabel) {
          const t = (childLabel.innerText || childLabel.textContent || '').replace(/\s+/g, ' ').trim();
          if (t && !optionLabels.has(t.toLowerCase())) { groupLabel = t; break; }
        }

        // ── 2. Previous sibling is or contains the question label ─────────────
        // Handles Lever's pattern: <label>Question?</label> then <ul>options</ul>
        // where the label is a sibling of the options container, not its parent.
        const prevSib = p.previousElementSibling;
        if (prevSib) {
          let sibText = '';
          if (prevSib.tagName === 'LABEL' || /^H[1-6]$/.test(prevSib.tagName)) {
            const clone = prevSib.cloneNode(true);
            clone.querySelectorAll('input,select,textarea,button').forEach(c => c.remove());
            sibText = (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim();
          } else {
            // Look for a label/heading inside the sibling (one level deep)
            const inner = prevSib.querySelector('label, h1, h2, h3, h4, h5');
            if (inner) {
              const clone = inner.cloneNode(true);
              clone.querySelectorAll('input,select,textarea,button').forEach(c => c.remove());
              sibText = (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim();
            }
          }
          if (sibText && sibText.length > 5 && !optionLabels.has(sibText.toLowerCase())) {
            groupLabel = sibText;
            break;
          }
        }

        // ── 3. aria-labelledby ───────────────────────────────────────────────
        const lblBy = p.getAttribute('aria-labelledby');
        if (lblBy) {
          const lbEl = document.getElementById(lblBy);
          if (lbEl) { groupLabel = (lbEl.innerText || lbEl.textContent || '').replace(/\s+/g, ' ').trim(); if (groupLabel) break; }
        }

        // ── 4. <div role="group"> aria-label ────────────────────────────────
        if (p.getAttribute('role') === 'group') {
          const al = p.getAttribute('aria-label');
          if (al) { groupLabel = al.trim(); break; }
        }

        // ── 5. Direct child heading (h1-h5) — section-level heading ─────────
        for (const hTag of ['h1', 'h2', 'h3', 'h4', 'h5']) {
          const h = p.querySelector(':scope > ' + hTag);
          if (h) {
            const ht = (h.innerText || h.textContent || '').replace(/\s+/g, ' ').trim();
            if (ht && !optionLabels.has(ht.toLowerCase())) { groupLabel = ht; break; }
          }
          if (groupLabel) break;
        }
        if (groupLabel) break;

        // ── 6. <fieldset> + <legend> (least specific — section wrapper) ──────
        if (p.tagName === 'FIELDSET') {
          const leg = p.querySelector(':scope > legend');
          if (leg) { groupLabel = (leg.innerText || leg.textContent || '').trim(); break; }
        }

        p = p.parentElement;
        depth++;
      }
    }
    // Fallback: convert camelCase / snake_case name attribute to human label
    if (!groupLabel) {
      groupLabel = groupName.replace(/([A-Z])/g, ' $1').replace(/[_-]/g, ' ').trim();
    }

    results.push({
      selector: selector,
      label_text: groupLabel.toLowerCase(),
      field_type: 'radio',
      name: groupName,
      id: '',
      placeholder: '',
      required: false,
      current_value: '',
      radio_options: group.options.map(o => o.label || o.value),
      select_options: [],
      group_name: groupName,
    });
  }

  return results;
}
