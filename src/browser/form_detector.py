"""
Universal form field detector — portal-agnostic.

Injects JavaScript into the live page to find every visible input, textarea,
and select element regardless of the ATS platform. Works across:
  - Standard HTML forms (Greenhouse, Lever, Ashby)
  - Workday (Shadow DOM Web Components)
  - SPAs (React / Vue / Angular controlled inputs)
  - Any custom portal without known selector maps

No hardcoded CSS selectors. The JS traverses the live DOM and returns a
structured description of every fillable field.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DetectedField:
    selector: str           # unique CSS selector for this element
    label_text: str         # associated label, lowercase stripped
    field_type: str         # text|email|tel|url|number|textarea|select|radio|checkbox|file
    name: str               # name attribute
    id: str                 # id attribute
    placeholder: str        # placeholder attribute, lowercase
    required: bool
    current_value: str      # already-filled value (skip if non-empty)
    radio_options: list     # for radio groups: list of option label strings
    select_options: list    # for <select>: list of option text strings
    group_name: str         # for radio groups: the name/fieldset identifier


# ---------------------------------------------------------------------------
# JavaScript injected into the page via page.evaluate()
# ---------------------------------------------------------------------------

_JS_EXTRACTOR = r"""
() => {
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
    try {
      const style = window.getComputedStyle(el);
      if (style.display === 'none') return false;
      if (style.visibility === 'hidden') return false;
      if (parseFloat(style.opacity) < 0.01) return false;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 && rect.height <= 0) return false;
      if (rect.right < -50 || rect.bottom < -50) return false;
    } catch(e) { return false; }

    // Check aria-hidden on ancestors
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
    // Index-based fallback
    const tag = el.tagName.toLowerCase();
    const siblings = Array.from(document.querySelectorAll(tag));
    const idx = siblings.indexOf(el);
    return idx >= 0 ? tag + ':nth-of-type(' + (idx + 1) + ')' : tag;
  }

  // Recursively collect form elements including Shadow DOM
  function collectElements(root, domRoot) {
    const els = root.querySelectorAll('input, textarea, select');
    for (const el of els) {
      processElement(el, domRoot || root);
    }
    // Recurse into shadow roots
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
    if (safeAttr(el, 'tabindex') === '-1') return;
    if (!isVisible(el)) return;

    const label = (getLabel(el, domRoot) || '').trim().replace(/\s+/g, ' ');
    const dedupeKey = tag + ':' + (el.name || '') + ':' + (el.id || '') + ':' + label;
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);

    if (type === 'radio') {
      const groupName = el.name || el.id || label;
      if (!radioGroups[groupName]) radioGroups[groupName] = {label: label, options: []};
      const optLabel = safeAttr(el, 'aria-label') ||
                       (el.id && domRoot.querySelector('label[for="' + el.id + '"]')
                          ? (domRoot.querySelector('label[for="' + el.id + '"]').innerText || '').trim()
                          : '') ||
                       el.value || '';
      radioGroups[groupName].options.push({label: optLabel.trim(), value: el.value});
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
    // Use first radio's selector as the representative
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
"""


def extract_form_fields(page) -> list[DetectedField]:
    """
    Run JS field extractor on the live page.
    Returns a list of DetectedField objects, or [] on any error.
    Never raises.
    """
    try:
        raw: list[dict] = page.evaluate(_JS_EXTRACTOR)
        return _parse_js_result(raw)
    except Exception as exc:
        logger.warning("form_detector: JS evaluation failed: %s", exc)
        return []


def _parse_js_result(raw: list[dict]) -> list[DetectedField]:
    fields = []
    for item in raw:
        try:
            fields.append(DetectedField(
                selector=item.get("selector", ""),
                label_text=(item.get("label_text") or "").strip(),
                field_type=item.get("field_type", "text"),
                name=item.get("name", ""),
                id=item.get("id", ""),
                placeholder=(item.get("placeholder") or "").strip(),
                required=bool(item.get("required", False)),
                current_value=item.get("current_value", ""),
                radio_options=item.get("radio_options", []),
                select_options=item.get("select_options", []),
                group_name=item.get("group_name", ""),
            ))
        except Exception:
            pass
    return fields
