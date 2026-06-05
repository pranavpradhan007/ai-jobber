"""
Central runtime config for the job agent.
Values are read from environment variables, falling back to defaults.
"""
import os

# Model used for all LLM calls (scorer, rephraser, extractor, classifier).
# Change this one constant to switch models globally.
# Options: claude-haiku-4-5-20251001 | claude-sonnet-4-6 | claude-opus-4-8
DEFAULT_MODEL: str = os.environ.get(
    "JOB_AGENT_MODEL", "claude-haiku-4-5-20251001"
)

# Candidate name in generated resumes (override via CLI --candidate-name)
CANDIDATE_NAME: str = os.environ.get(
    "JOB_AGENT_CANDIDATE_NAME", "Pranav Tushar Pradhan"
)

# Application answers file for browser prefill
APPLICATION_ANSWERS_PATH: str = os.environ.get(
    "JOB_AGENT_ANSWERS",
    "knowledge_base/profile/application_answers.json",
)

# Continuous loop settings
LOOP_INTERVAL_MINUTES: int = int(os.environ.get("JOB_AGENT_LOOP_INTERVAL", "30"))
MAX_JOBS_PER_CYCLE: int    = int(os.environ.get("JOB_AGENT_MAX_JOBS_PER_CYCLE", "20"))

# Resume paths (used when not tailoring)
RESUME_DOCX_PATH: str = os.environ.get(
    "JOB_AGENT_RESUME_DOCX",
    r"D:\Pranav\Resume\New folder\ML-AI Resume.docx",
)
RESUME_PDF_PATH: str = os.environ.get(
    "JOB_AGENT_RESUME_PDF",
    r"D:\Pranav\Resume\New folder\Pranav ML-AI Resume.pdf",
)
