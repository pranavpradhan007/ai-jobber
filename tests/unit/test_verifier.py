"""
Unit tests for Sprint 2: source_bank, retrieval, diff-verifier, hard gate.
All LLM calls replaced by deterministic mock extractors.
"""
import pytest

from src.db.jobs import create_job
from src.db.applications import create_application, update_application
from src.storage.bank_loader import seed_from_yaml
from src.verifier.retrieval import (
    get_bank_item,
    retrieve_for_surface,
    check_item_for_surface,
    is_permitted,
    UsageLevelViolation,
)
from src.verifier.diff_verifier import (
    verify_text,
    verifier_hard_gate,
    VerifierGateError,
)


# ── Mock extractors ─────────────────────────────────────────────────────────

def extractor_python_and_metric(sentence: str) -> list[dict]:
    """Returns 'Python' (tool) + 'Reduced API latency by 40%' (metric) for any sentence."""
    return [
        {"text": "Python", "type": "tool"},
        {"text": "Reduced API latency by 40%", "type": "metric"},
    ]


def extractor_fake_metric(sentence: str) -> list[dict]:
    """Injects a metric not in source_bank."""
    return [{"text": "Increased revenue by 500%", "type": "metric"}]


def extractor_fake_tool(sentence: str) -> list[dict]:
    """Injects a tool not in source_bank."""
    return [{"text": "SuperSecretFramework", "type": "tool"}]


def extractor_private_item(sentence: str) -> list[dict]:
    """Returns 'Staff Engineer' which is private_never_submit."""
    return [{"text": "Staff Engineer", "type": "title"}]


def extractor_no_claims(sentence: str) -> list[dict]:
    return []


def extractor_dbt_for_screener(sentence: str) -> list[dict]:
    """dbt is cover_letter level — should be blocked on screener surface."""
    return [{"text": "dbt", "type": "tool"}]


# ── 1. Bank seeding ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_seed_inserts_items(tmp_db, tmp_path):
    """seed_from_yaml must populate source_bank."""
    import os
    fixture = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "sample_source_bank.yaml"
    )
    count = seed_from_yaml(tmp_db, fixture)
    assert count > 0

    cur = tmp_db.execute("SELECT COUNT(*) FROM source_bank")
    assert cur.fetchone()[0] == count


@pytest.mark.unit
def test_seed_is_idempotent(seeded_db):
    """Seeding twice must not raise or duplicate rows."""
    import os
    fixture = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "sample_source_bank.yaml"
    )
    before = seeded_db.execute("SELECT COUNT(*) FROM source_bank").fetchone()[0]
    seed_from_yaml(seeded_db, fixture)
    after = seeded_db.execute("SELECT COUNT(*) FROM source_bank").fetchone()[0]
    assert before == after


# ── 2. Retrieval API ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_get_bank_item_found(seeded_db):
    item = get_bank_item(seeded_db, "Python")
    assert item is not None
    assert item.item_type == "tool"


@pytest.mark.unit
def test_get_bank_item_case_insensitive(seeded_db):
    item = get_bank_item(seeded_db, "python")
    assert item is not None


@pytest.mark.unit
def test_get_bank_item_not_found(seeded_db):
    item = get_bank_item(seeded_db, "NonExistentTool9999")
    assert item is None


@pytest.mark.unit
def test_retrieve_for_resume_surface(seeded_db):
    """resume surface should include resume-level items."""
    items = retrieve_for_surface(seeded_db, "resume")
    types = {i.usage_level for i in items}
    assert "resume" in types
    # private_never_submit must never appear
    assert "private_never_submit" not in types


@pytest.mark.unit
def test_private_never_submit_excluded_from_all_surfaces(seeded_db):
    """Staff Engineer (private_never_submit) must never appear on any surface."""
    for surface in ("resume", "cover_letter", "screener"):
        items = retrieve_for_surface(seeded_db, surface)
        contents = [i.content for i in items]
        assert "Staff Engineer" not in contents, \
            f"private_never_submit item appeared on surface={surface}"


@pytest.mark.unit
def test_screener_only_item_not_on_resume(seeded_db):
    """Agile (screener) must not appear in resume surface results."""
    items = retrieve_for_surface(seeded_db, "resume")
    contents = [i.content for i in items]
    assert "Agile" not in contents


@pytest.mark.unit
def test_screener_item_on_screener_surface(seeded_db):
    """Agile (screener) must appear on screener surface."""
    items = retrieve_for_surface(seeded_db, "screener")
    contents = [i.content for i in items]
    assert "Agile" in contents


@pytest.mark.unit
def test_is_permitted():
    assert is_permitted("resume", "resume") is True
    assert is_permitted("resume", "cover_letter") is True
    assert is_permitted("resume", "screener") is True
    assert is_permitted("cover_letter", "resume") is False
    assert is_permitted("cover_letter", "cover_letter") is True
    assert is_permitted("screener", "resume") is False
    assert is_permitted("screener", "screener") is True
    assert is_permitted("private_never_submit", "resume") is False
    assert is_permitted("private_never_submit", "screener") is False


@pytest.mark.unit
def test_check_item_for_surface_blocks_private(seeded_db):
    """check_item_for_surface must raise UsageLevelViolation for private item."""
    with pytest.raises(UsageLevelViolation):
        check_item_for_surface(seeded_db, "Staff Engineer", "resume")


@pytest.mark.unit
def test_check_item_for_surface_allows_valid(seeded_db):
    """check_item_for_surface must return the item for a valid surface."""
    item = check_item_for_surface(seeded_db, "Python", "resume")
    assert item.content == "Python"


# ── 3. Diff-verifier — clean rephrase passes ─────────────────────────────────

@pytest.mark.unit
def test_clean_rephrase_passes(seeded_db):
    """Text with only bank-approved claims must produce passed=True."""
    job = create_job(seeded_db, source="test", url="https://v.test/1")
    app = create_application(seeded_db, job_id=job.id)

    report = verify_text(
        seeded_db, app.id,
        "Implemented Python services and Reduced API latency by 40%.",
        surface="resume",
        extractor=extractor_python_and_metric,
    )
    assert report.passed is True
    assert len(report.blocked_claims) == 0


# ── 4. Injected fake metric is blocked ────────────────────────────────────────

@pytest.mark.unit
def test_fake_metric_blocked(seeded_db):
    """A metric not in source_bank must be blocked and marked needs_approval."""
    job = create_job(seeded_db, source="test", url="https://v.test/2")
    app = create_application(seeded_db, job_id=job.id)

    report = verify_text(
        seeded_db, app.id,
        "Increased revenue by 500% using a proprietary system.",
        surface="resume",
        extractor=extractor_fake_metric,
    )
    assert report.passed is False
    assert len(report.blocked_claims) == 1
    assert report.blocked_claims[0].claim_text == "Increased revenue by 500%"
    assert report.blocked_claims[0].needs_approval is True


@pytest.mark.unit
def test_fake_tool_blocked(seeded_db):
    """A tool not in source_bank must be blocked."""
    job = create_job(seeded_db, source="test", url="https://v.test/3")
    app = create_application(seeded_db, job_id=job.id)

    report = verify_text(
        seeded_db, app.id,
        "Expert in SuperSecretFramework.",
        surface="resume",
        extractor=extractor_fake_tool,
    )
    assert report.passed is False


# ── 5. private_never_submit cannot reach a resume ────────────────────────────

@pytest.mark.unit
def test_private_never_submit_blocked_on_resume(seeded_db):
    """'Staff Engineer' (private_never_submit) must be blocked even if it's in the bank."""
    job = create_job(seeded_db, source="test", url="https://v.test/4")
    app = create_application(seeded_db, job_id=job.id)

    report = verify_text(
        seeded_db, app.id,
        "Previously worked as Staff Engineer.",
        surface="resume",
        extractor=extractor_private_item,
    )
    assert report.passed is False
    blocked = report.blocked_claims
    assert any(c.claim_text == "Staff Engineer" for c in blocked)


# ── 6. usage_level enforced at retrieval ─────────────────────────────────────

@pytest.mark.unit
def test_cover_letter_item_blocked_on_resume(seeded_db):
    """dbt (cover_letter level) must be blocked on resume surface."""
    job = create_job(seeded_db, source="test", url="https://v.test/5")
    app = create_application(seeded_db, job_id=job.id)

    report = verify_text(
        seeded_db, app.id,
        "Proficient in dbt for analytics.",
        surface="resume",
        extractor=extractor_dbt_for_screener,
    )
    assert report.passed is False


# ── 7. Hard gate ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_hard_gate_passes_when_no_blocked_claims(seeded_db):
    """verifier_hard_gate must not raise when all claims are allowed."""
    job = create_job(seeded_db, source="test", url="https://v.test/6")
    app = create_application(seeded_db, job_id=job.id)

    verify_text(
        seeded_db, app.id,
        "Used Python.",
        surface="resume",
        extractor=lambda s: [{"text": "Python", "type": "tool"}],
    )
    # Should not raise
    verifier_hard_gate(seeded_db, app.id)


@pytest.mark.unit
def test_hard_gate_raises_when_blocked_claim_exists(seeded_db):
    """verifier_hard_gate must raise VerifierGateError when a claim is blocked."""
    job = create_job(seeded_db, source="test", url="https://v.test/7")
    app = create_application(seeded_db, job_id=job.id)

    verify_text(
        seeded_db, app.id,
        "Expert in SuperSecretFramework.",
        surface="resume",
        extractor=extractor_fake_tool,
    )
    with pytest.raises(VerifierGateError):
        verifier_hard_gate(seeded_db, app.id)


@pytest.mark.unit
def test_hard_gate_new_app_no_claims_passes(seeded_db):
    """An application with no claims at all must pass the hard gate."""
    job = create_job(seeded_db, source="test", url="https://v.test/8")
    app = create_application(seeded_db, job_id=job.id)
    # No verify_text called — no claims in DB
    verifier_hard_gate(seeded_db, app.id)  # must not raise


# ── 8. High-stakes blocked claims create approvals records ───────────────────

@pytest.mark.unit
def test_blocked_metric_creates_approval_record(seeded_db):
    """A blocked metric must create a pending approvals record."""
    job = create_job(seeded_db, source="test", url="https://v.test/9")
    app = create_application(seeded_db, job_id=job.id)

    verify_text(
        seeded_db, app.id,
        "Boosted sales by 300%.",
        surface="resume",
        extractor=lambda s: [{"text": "Boosted sales by 300%", "type": "metric"}],
    )
    cur = seeded_db.execute(
        "SELECT COUNT(*) FROM approvals WHERE application_id=? AND status='pending'",
        (app.id,),
    )
    assert cur.fetchone()[0] >= 1
