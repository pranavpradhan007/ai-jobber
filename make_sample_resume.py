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

from src.resume.resume_agent import run_resume_agent
from src.resume.ats_agent import run_ats_check

CACHE = OUT_DIR / "reframe_cache.json"

print(f"\nRunning Resume Agent (up to 15 attempts) for: {JOB_TITLE!r}")
print(f"Hot keywords: {', '.join(HOT_KEYWORDS[:8])}...")
print()

resume_result = run_resume_agent(
    source_docx=SOURCE_DOCX,
    source_pdf=r"D:\Pranav\Resume\New folder\Pranav ML-AI Resume.pdf",
    docx_path=str(OUT_DOCX),
    pdf_path=str(OUT_PDF),
    hot_keywords=HOT_KEYWORDS,
    job_title=JOB_TITLE,
    raw_jd="",
    rephraser_fn=None,
    cache_path=str(CACHE) if CACHE.is_file() else None,
    max_attempts=15,
    score_threshold=85.0,
)

print(f"Resume Agent: success={resume_result.success}  attempts={resume_result.total_attempts}")
for att in resume_result.all_attempts:
    status = "PASS" if (att.checker.passed and att.score >= 85.0) else "FAIL"
    print(f"  [{status}] attempt={att.attempt} strategy={att.strategy} "
          f"rules={'OK' if att.checker.passed else 'FAIL'} "
          f"score={att.score}/100 kw={att.keywords_injected}")

SHOW_PDF = str(OUT_PDF)
GT_PDF   = r"D:\Pranav\Resume\New folder\Pranav ML-AI Resume.pdf"

if not resume_result.success:
    print("\nResume Agent FAILED all 15 attempts — showing ground truth PDF.")
    SHOW_PDF = GT_PDF
else:
    best = resume_result.best
    print(f"\nBest resume: attempt={best.attempt} score={best.score} strategy={best.strategy}")

    # Copy best artifacts to OUT_DOCX / OUT_PDF
    if best.docx_path != str(OUT_DOCX):
        shutil.copy2(best.docx_path, str(OUT_DOCX))
    if best.pdf_path != str(OUT_PDF):
        shutil.copy2(best.pdf_path, str(OUT_PDF))

    # Calibrate ATS threshold against ground truth
    from src.resume.ats_agent import ground_truth_ats_score
    gt_ats = ground_truth_ats_score(SOURCE_DOCX, GT_PDF, HOT_KEYWORDS)
    ats_threshold = max(60.0, gt_ats - 5.0)
    print(f"\nGround truth ATS score: {gt_ats:.1f}")
    print(f"ATS acceptance threshold: {ats_threshold:.1f}")

    # ATS check on tailored version
    print("\nRunning ATS Agent on tailored resume...")
    ats = run_ats_check(
        pdf_path=str(OUT_PDF),
        docx_path=str(OUT_DOCX),
        hot_keywords=HOT_KEYWORDS,
        min_score=ats_threshold,
    )
    print(ats)

    if ats.passed:
        print(f"\nATS PASSED ({ats.score:.1f}/{ats_threshold:.1f}) — tailored resume accepted.")
    else:
        print(f"\nATS FAILED ({ats.score:.1f}/{ats_threshold:.1f}) — showing ground truth PDF.")
        SHOW_PDF = GT_PDF

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
