"""
Job description parser.
Produces: clean_jd (normalised text), jd_hash (SHA-256), structured fields.
No model calls — pure text processing.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedJD:
    raw_jd: str
    clean_jd: str
    jd_hash: str
    title_hint: Optional[str] = None   # extracted from posting if present
    location_hint: Optional[str] = None


def parse_jd(raw_jd: str) -> ParsedJD:
    """
    Clean and hash a raw job description.
    Returns a ParsedJD with normalised text and SHA-256 hash.
    """
    clean = _clean(raw_jd)
    jd_hash = _sha256(clean)
    title_hint = _extract_title(raw_jd)
    location_hint = _extract_location(raw_jd)
    return ParsedJD(
        raw_jd=raw_jd,
        clean_jd=clean,
        jd_hash=jd_hash,
        title_hint=title_hint,
        location_hint=location_hint,
    )


def _clean(text: str) -> str:
    """Normalise unicode, collapse whitespace, strip HTML tags."""
    # Normalise unicode
    text = unicodedata.normalize("NFKC", text)
    # Strip HTML/XML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">").replace("&nbsp;", " ").replace("&#39;", "'")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_title(text: str) -> Optional[str]:
    """Heuristic: look for 'Job Title:' or the first capitalised phrase."""
    m = re.search(
        r"(?i)(?:job\s+title|position|role)\s*[:\-]\s*([^\n\r]{3,80})",
        text,
    )
    if m:
        return m.group(1).strip()
    return None


def _extract_location(text: str) -> Optional[str]:
    m = re.search(
        r"(?i)(?:location|office|city)\s*[:\-]\s*([^\n\r]{3,60})",
        text,
    )
    if m:
        return m.group(1).strip()
    return None
