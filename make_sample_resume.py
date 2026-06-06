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
pdf_path = render_pdf(str(OUT_DOCX), str(OUT_PDF))
print(f"PDF saved: {pdf_path}")

# ── Resume checker ────────────────────────────────────────────────────────────

from src.resume.checker import check_resume

print("\nRunning resume checker...")
check = check_resume(str(OUT_PDF), str(OUT_DOCX), hot_keywords=HOT_KEYWORDS)
print(check)

SHOW_PDF = str(OUT_PDF)

if not check.passed:
    print("\nChecker FAILED — falling back to ground truth PDF for display.")
    gt_pdf = r"D:\Pranav\Resume\New folder\Pranav ML-AI Resume.pdf"
    if pathlib.Path(gt_pdf).is_file():
        SHOW_PDF = gt_pdf
        print(f"Showing ground truth PDF: {SHOW_PDF}")
    else:
        print(f"Ground truth PDF not found at {gt_pdf}, showing tailored anyway.")
else:
    print("\nChecker PASSED — tailored resume is clean and 1 page.")

# ── Open in Chrome ────────────────────────────────────────────────────────────

chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
abs_pdf = str(pathlib.Path(SHOW_PDF).resolve())

print(f"\nOpening PDF in Chrome: {abs_pdf}")
try:
    subprocess.Popen([chrome_exe, abs_pdf])
    print("Chrome launched — review your resume.")
except Exception as exc:
    print(f"Could not open Chrome ({exc}).")
    print(f"Open this file manually: {abs_pdf}")
