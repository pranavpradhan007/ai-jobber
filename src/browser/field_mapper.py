"""
Field mapper.

Maps structured application answers (from application_answers.json) to
portal-specific CSS selectors / form field names.

Returns a list of FillInstruction objects that the pre-fill engine executes.
"""
from __future__ import annotations
import html as html_lib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

FILL_DELAY_SECONDS = 2.0   # minimum delay between fills (runtime); 0 in tests


@dataclass
class FillInstruction:
    selector: str         # CSS selector or field name
    value: str            # value to type/select
    field_type: str       # 'text' | 'select' | 'checkbox' | 'file' | 'textarea'
    label: str            # human-readable label (for logging)
    required: bool = False


# ---------------------------------------------------------------------------
# Portal-specific selector maps
# ---------------------------------------------------------------------------

_WORKDAY_SELECTORS: dict[str, str] = {
    "first_name":           "#firstName",
    "last_name":            "#lastName",
    "email":                "#email",
    "phone":                "#phone",
    "linkedin_url":         "#linkedinUrl",
    "years_experience":     "#yearsExperience",
    "work_authorization":   "#workAuthorization",
    "cover_letter":         "#coverLetter",
}

_GREENHOUSE_SELECTORS: dict[str, str] = {
    "first_name":   "#first_name",
    "last_name":    "#last_name",
    "email":        "#email",
    "phone":        "#phone",
    "cover_letter": "#cover_letter",
    "location":     "#location",
}

_LEVER_SELECTORS: dict[str, str] = {
    "first_name":   "input[name='name']",
    "last_name":    "input[name='org']",     # lever uses org for company but some use name split
    "email":        "input[name='email']",
    "phone":        "input[name='phone']",
    "cover_letter": "textarea[name='comments']",
}

_ASHBY_SELECTORS: dict[str, str] = {
    "first_name":   "input[data-field='firstName']",
    "last_name":    "input[data-field='lastName']",
    "email":        "input[data-field='email']",
    "phone":        "input[data-field='phone']",
    "cover_letter": "textarea[data-field='coverLetter']",
}

_PORTAL_MAPS: dict[str, dict] = {
    "workday":    _WORKDAY_SELECTORS,
    "greenhouse": _GREENHOUSE_SELECTORS,
    "lever":      _LEVER_SELECTORS,
    "ashby":      _ASHBY_SELECTORS,
    "custom":     _GREENHOUSE_SELECTORS,   # generic fallback
}

# Answer key → field_type hint
_FIELD_TYPES: dict[str, str] = {
    "first_name":         "text",
    "last_name":          "text",
    "email":              "text",
    "phone":              "text",
    "linkedin_url":       "text",
    "years_experience":   "select",
    "work_authorization": "select",
    "cover_letter":       "textarea",
    "location":           "text",
    "resume":             "file",
}


def build_fill_instructions(
    answers: dict,
    portal: str,
) -> list[FillInstruction]:
    """
    Build a list of FillInstruction objects for the given portal.
    `answers` is the parsed application_answers.json dict.
    """
    selector_map = _PORTAL_MAPS.get(portal, _GREENHOUSE_SELECTORS)
    instructions: list[FillInstruction] = []

    for key, value in answers.items():
        if not value or key == "resume":   # resume handled separately
            continue
        selector = selector_map.get(key)
        if selector is None:
            continue   # unknown field for this portal — skip
        field_type = _FIELD_TYPES.get(key, "text")
        instructions.append(FillInstruction(
            selector=selector,
            value=str(value),
            field_type=field_type,
            label=key,
            required=(key in ("first_name", "last_name", "email")),
        ))

    return instructions


# ---------------------------------------------------------------------------
# Static HTML field discovery (used in tests — no live browser)
# ---------------------------------------------------------------------------

class _FormParser(HTMLParser):
    """Minimal HTML parser to extract form fields from static HTML."""

    def __init__(self):
        super().__init__()
        self.fields: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("input", "select", "textarea"):
            return
        a = dict(attrs)
        self.fields.append({
            "tag":  tag,
            "id":   a.get("id", ""),
            "name": a.get("name", ""),
            "type": a.get("type", "text"),
        })


def discover_fields_from_html(html: str) -> list[dict]:
    """Parse static HTML and return a list of form field descriptors."""
    parser = _FormParser()
    parser.feed(html)
    return parser.fields
