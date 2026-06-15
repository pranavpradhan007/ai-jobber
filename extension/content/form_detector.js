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
        return (parent.innerText || parent.textContent || '').replace(/\s+/g, ' ');
      }
      const childLbl = parent.querySelector(':scope > label');
      if (childLbl) {
        return (childLbl.innerText || childLbl.textContent || '').replace(/\s+/g, ' ');
      }
      parent = parent.parentElement;
      depth++;
    }

    // 5. Fallback: placeholder or name
    return el.placeholder || el.name || '';
  }

  function isVisible(el) {
    const elType = (el.type || '').toLowerCase();
    // Radio and checkbox inputs are routinely visually-hidden behind styled labels
    // (screen-reader accessibility pattern used by Ashby, Lever, Greenhouse, etc.)
    // Don't apply bounding-box or opacity checks for them.
    const isToggle = (elType === 'radio' || elType === 'checkbox');

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
    // Allow tabindex=-1 on radio/checkbox (Ashby hides them with tabindex=-1 + CSS)
    if (safeAttr(el, 'tabindex') === '-1' && type !== 'radio' && type !== 'checkbox') return;
    if (!isVisible(el)) return;

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
    results.push({
      selector: selector,
      label_text: group.label.toLowerCase(),
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
