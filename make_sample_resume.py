"""
Generate a sample tailored resume and open it in Chrome.

Uses the ground-truth DOCX as source, duplicates it, reframes bullets
for a sample ML Engineer JD, injects keywords, and converts to PDF.

Run: python make_sample_resume.py
"""
import os
import sys
import shutil
import pathlib
import subprocess

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load .env so ANTHROPIC_API_KEY is available before any src imports
from dotenv import load_dotenv  # noqa: E402
load_dotenv(pathlib.Path(".env").resolve(), override=True)

# ── Config ────────────────────────────────────────────────────────────────────

SOURCE_DOCX = r"D:\Pranav\Resume\New folder\ML-AI Resume.docx"
OUT_DIR     = pathlib.Path("applications") / "sample"
OUT_DOCX    = OUT_DIR / "resume_sample.docx"
OUT_PDF     = OUT_DIR / "resume_sample.pdf"

# Sample JD for an ML Engineer role
JOB_TITLE = "Machine Learning Engineer"
HOT_KEYWORDS = [
    "PyTorch", "distributed training", "RLHF", "LLM fine-tuning",
    "model inference", "RAG", "evaluation pipelines", "MLflow",
    "Triton", "vLLM", "PEFT", "LoRA", "quantization",
    "production ML systems", "model serving",
]

# ── Setup ─────────────────────────────────────────────────────────────────────

OUT_DIR.mkdir(parents=True, exist_ok=True)

if not pathlib.Path(SOURCE_DOCX).is_file():
    print(f"ERROR: Ground truth resume not found at: {SOURCE_DOCX}")
    sys.exit(1)

# Duplicate — never touch the source
shutil.copy2(SOURCE_DOCX, OUT_DOCX)
print(f"Copied ground-truth DOCX to: {OUT_DOCX}")

# ── Reframe bullets ───────────────────────────────────────────────────────────

from src.resume.bullet_reframer import reframe_bullets_in_docx

print(f"\nReframing bullets for: {JOB_TITLE!r}")
print(f"Hot keywords: {', '.join(HOT_KEYWORDS[:8])}...")

CACHE = OUT_DIR / "reframe_cache.json"
n = reframe_bullets_in_docx(
    str(OUT_DOCX),
    hot_keywords=HOT_KEYWORDS,
    job_title=JOB_TITLE,
    rephraser_fn=None,
    cache_path=str(CACHE) if CACHE.is_file() else None,
)
print(f"Reframed {n} bullets.")

# ── Inject skills keywords ────────────────────────────────────────────────────

from src.resume.pipeline import _inject_keywords

added = _inject_keywords(str(OUT_DOCX), HOT_KEYWORDS)
if added:
    print(f"Injected {len(added)} new skills keywords: {', '.join(added)}")
else:
    print("No new keywords needed in skills line (all already present).")

# ── Convert to PDF ────────────────────────────────────────────────────────────

from src.resume.renderer import render_pdf

print(f"\nConverting to PDF...")
try:
    pdf_path = render_pdf(str(OUT_DOCX), str(OUT_PDF))
    print(f"PDF saved: {pdf_path}")
except Exception as exc:
    # Fallback: try LibreOffice
    print(f"render_pdf failed ({exc}), trying LibreOffice...")
    try:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf",
             "--outdir", str(OUT_DIR), str(OUT_DOCX)],
            check=True, capture_output=True,
        )
        # LibreOffice names it after the DOCX stem
        lo_out = OUT_DIR / "resume_sample.pdf"
        pdf_path = str(lo_out) if lo_out.is_file() else ""
        if pdf_path:
            print(f"PDF saved via LibreOffice: {pdf_path}")
        else:
            print("LibreOffice conversion produced no output file.")
            sys.exit(1)
    except Exception as exc2:
        print(f"LibreOffice also failed: {exc2}")
        print(f"\nThe DOCX is ready at: {OUT_DOCX}")
        print("Open it in Word to view.")
        sys.exit(0)

# ── Open in Chrome ────────────────────────────────────────────────────────────

chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
abs_pdf = str(pathlib.Path(pdf_path).resolve())

print(f"\nOpening PDF in Chrome: {abs_pdf}")
try:
    subprocess.Popen([chrome_exe, abs_pdf])
    print("Chrome launched — review your tailored resume.")
except Exception as exc:
    print(f"Could not open Chrome ({exc}).")
    print(f"Open this file manually: {abs_pdf}")
