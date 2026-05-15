"""
M3 acceptance tests for the ASAM risk-ratings computer.

Two flavors of coverage:

  1. **Snapshot unit tests** that bypass the DB by constructing a
     ``ClinicalSnapshot`` with hand-picked rows. These are fast, deterministic,
     and let us probe each helper in isolation.
  2. **Live ingested-chart test** that runs ``compute_risk_ratings`` against
     the Phase 1 ``ingested_patient`` fixture, asserting Marcus's expected
     per-subdimension ratings. This is the demo-defining test for Phase 3:
     if these ratings ever change unexpectedly, the M4 decision tree's
     output will change too.

The implementation plan's "Marcus day-8 step-down" fixture is deferred to
M12 (golden tests), where it'll be a fixture-controlled snapshot rather
than a live re-ingestion.
"""

from datetime import date, datetime
from uuid import uuid4

from app.clinical.asam.risk_ratings import (
    ClinicalSnapshot,
    _interpret_cssrs,
    _rate_dim1_addiction_meds,
    _rate_dim1_withdrawal,
    _rate_dim3_active_psych,
    _rate_dim4_risky_use,
    compute_risk_ratings,
)
from app.clinical.asam.rubric import (
    VALID_RATINGS_BY_SUBDIM,
    RiskRating,
    Subdimension,
)
from app.db.models import ClinicalDocument, ExtractedObservation, Patient

# ─── Helpers for snapshot construction ───────────────────────────────────


def _make_patient(gender: str = "male") -> Patient:
    return Patient(
        id=uuid4(),
        external_id="TEST-MARCUS",
        given_name="Marcus",
        family_name="Reyes",
        birth_date=date(1991, 11, 4),
        gender=gender,
    )


def _make_doc(patient_id, authored_on: datetime, doc_type: str = "bps_intake") -> ClinicalDocument:
    return ClinicalDocument(
        id=uuid4(),
        patient_id=patient_id,
        encounter_id=uuid4(),
        document_type=doc_type,
        authored_on=authored_on,
        author_name="Dr Test",
        author_role="psychiatrist",
        raw_text="...",
        content_hash=f"hash-{uuid4()}",
    )


def _make_obs(
    document_id,
    code_system: str,
    code: str,
    *,
    value_quantity: float | None = None,
    value_string: str | None = None,
    char_start: int = 0,
    char_end: int = 10,
) -> ExtractedObservation:
    return ExtractedObservation(
        id=uuid4(),
        document_id=document_id,
        code_system=code_system,
        code=code,
        display=f"{code_system} {code}",
        value_quantity=value_quantity,
        value_string=value_string,
        char_start=char_start,
        char_end=char_end,
        extraction_method="regex",
        confidence=1.0,
    )


# ─── C-SSRS interpreter unit tests ───────────────────────────────────────


def test_cssrs_marcus_passive_ideation_no_active_si():
    """Marcus's actual chart phrasing -> passive ideation, no active SI."""
    active, plan, passive = _interpret_cssrs(
        "low-intensity passive ideation; no active SI/plan/intent."
    )
    assert (active, plan, passive) == (False, False, True)


def test_cssrs_active_with_plan_and_intent():
    """Active SI with plan + intent -> the severe branch."""
    active, plan, passive = _interpret_cssrs(
        "active suicidal ideation with plan and intent; admit for safety"
    )
    assert active is True
    assert plan is True


def test_cssrs_explicit_denial_dominates():
    """'Denies SI' beats every other keyword."""
    active, plan, passive = _interpret_cssrs("denies SI; chronic passive ideation noted")
    assert (active, plan, passive) == (False, False, False)


def test_cssrs_empty_string_is_safe():
    """Missing C-SSRS data -> all flags False, no crash."""
    assert _interpret_cssrs("") == (False, False, False)


# ─── Snapshot-driven unit tests for the helpers ──────────────────────────


def test_dim1_withdrawal_marcus_admission_three_A():
    """CIWA-Ar=12 + concurrent alcohol+benzo dependence -> 3A (Min 3.7)."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 4, 13, 30))
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _make_obs(doc.id, "LOINC", "72109-3", value_quantity=12),  # CIWA-Ar admission
            _make_obs(doc.id, "ICD-10", "F10.232"),  # Alcohol UD severe + withdrawal
            _make_obs(doc.id, "ICD-10", "F13.20"),  # Sed/hypnotic UD severe (benzo)
        ],
    )
    assert _rate_dim1_withdrawal(snap) == RiskRating.DIM1_W_3A


def test_dim1_withdrawal_day8_stepdown_to_any():
    """CIWA-Ar=2 (post-stabilization) -> ANY."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 12, 11, 0), doc_type="dsap")
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_make_obs(doc.id, "LOINC", "72109-3", value_quantity=2)],
    )
    assert _rate_dim1_withdrawal(snap) == RiskRating.DIM1_W_ANY


def test_dim1_withdrawal_latest_wins_across_repeats():
    """Three CIWA-Ar readings (12, 8, 2) -> the latest by document date wins."""
    patient = _make_patient()
    doc_d1 = _make_doc(patient.id, datetime(2026, 5, 4, 13, 30), doc_type="bps_intake")
    doc_d3 = _make_doc(patient.id, datetime(2026, 5, 6, 11, 0), doc_type="soap")
    doc_d8 = _make_doc(patient.id, datetime(2026, 5, 12, 11, 0), doc_type="dsap")
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc_d1, doc_d3, doc_d8],
        observations=[
            _make_obs(doc_d1.id, "LOINC", "72109-3", value_quantity=12),
            _make_obs(doc_d3.id, "LOINC", "72109-3", value_quantity=8),
            _make_obs(doc_d8.id, "LOINC", "72109-3", value_quantity=2),
        ],
    )
    # Latest reading is 2 -> ANY. Demonstrates the day-8 step-down would
    # come from this helper if the AddictionMeds helper also stepped down.
    assert _rate_dim1_withdrawal(snap) == RiskRating.DIM1_W_ANY


def test_dim1_withdrawal_no_ciwa_with_sud_returns_eval():
    """No CIWA-Ar but SUD diagnosis present -> EVAL (needs evaluation)."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 4))
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_make_obs(doc.id, "ICD-10", "F10.232")],
    )
    assert _rate_dim1_withdrawal(snap) == RiskRating.DIM1_W_EVAL


def test_dim1_withdrawal_empty_chart_returns_zero():
    """No observations at all -> _0 (no need)."""
    patient = _make_patient()
    snap = ClinicalSnapshot(patient=patient)
    assert _rate_dim1_withdrawal(snap) == RiskRating.DIM1_W_0


def test_dim1_addiction_meds_benzo_taper_wins():
    """Active chlordiazepoxide prescription -> _C (Min 3.7)."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 4))
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _make_obs(doc.id, "internal", "medication:chlordiazepoxide"),
            _make_obs(doc.id, "internal", "medication:naltrexone"),  # anti-craving (would be _A)
        ],
    )
    # Benzo taper is the highest-intensity addiction medication -> wins.
    assert _rate_dim1_addiction_meds(snap) == RiskRating.DIM1_AM_C


def test_dim1_addiction_meds_no_meds_returns_zero():
    patient = _make_patient()
    snap = ClinicalSnapshot(patient=patient)
    assert _rate_dim1_addiction_meds(snap) == RiskRating.DIM1_AM_0


def test_dim3_active_psych_marcus_admission_one_B():
    """PHQ-9=18 + GAD-7=15 + C-SSRS passive only -> _1B (non-COE)."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 4))
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _make_obs(doc.id, "LOINC", "44261-6", value_quantity=18),  # PHQ-9
            _make_obs(doc.id, "LOINC", "70274-6", value_quantity=15),  # GAD-7
            _make_obs(
                doc.id,
                "LOINC",
                "93373-7",
                value_string="low-intensity passive ideation; no active SI/plan/intent.",
            ),
        ],
    )
    assert _rate_dim3_active_psych(snap) == RiskRating.DIM3_APS_1B


def test_dim3_active_psych_active_si_with_plan_routes_to_3A_COE():
    """Active SI + plan -> _3A_COE (the cautious severe anchor)."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 4))
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _make_obs(doc.id, "LOINC", "44261-6", value_quantity=22),
            _make_obs(doc.id, "LOINC", "70274-6", value_quantity=18),
            _make_obs(
                doc.id,
                "LOINC",
                "93373-7",
                value_string="active suicidal ideation with plan and intent",
            ),
        ],
    )
    assert _rate_dim3_active_psych(snap) == RiskRating.DIM3_APS_3A_COE


def test_dim3_active_psych_subclinical_returns_zero():
    patient = _make_patient()
    snap = ClinicalSnapshot(patient=patient)
    assert _rate_dim3_active_psych(snap) == RiskRating.DIM3_APS_0


def test_dim4_risky_use_marcus_high_audit_c_returns_E():
    """AUDIT-C=11 (>=8) -> _E (Min 3.5)."""
    patient = _make_patient()
    doc = _make_doc(patient.id, datetime(2026, 5, 4))
    snap = ClinicalSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _make_obs(doc.id, "LOINC", "75624-7", value_quantity=11),  # AUDIT-C
            _make_obs(doc.id, "LOINC", "82666-9", value_quantity=7),  # DAST-10
            _make_obs(doc.id, "ICD-10", "F10.232"),
        ],
    )
    assert _rate_dim4_risky_use(snap) == RiskRating.DIM4_RSU_E


# ─── End-to-end: every rating is in the rubric's valid set ───────────────


def test_empty_patient_returns_all_default_ratings(db_session):
    """A patient with no documents/observations -> 12 default ratings,
    every one in its subdimension's valid set."""
    patient = _make_patient()
    db_session.add(patient)
    db_session.flush()
    ratings = compute_risk_ratings(patient.id, db_session)
    assert set(ratings.keys()) == set(Subdimension)
    for subdim, rating in ratings.items():
        assert rating in VALID_RATINGS_BY_SUBDIM[subdim]


# ─── Demo-defining: against the live ingested Marcus chart ────────────────


def test_marcus_full_chart_yields_demo_ratings(ingested_patient, db_session):
    """The demo-critical assertion: compute_risk_ratings on Marcus's
    fully ingested chart produces the per-subdimension ratings the M4
    decision tree needs to land on Level 3.7 non-COE non-BIO.

    The combination that matters for the recommendation:
      * DIM1_AM = C -> Min 3.7 (active chlordiazepoxide taper)
      * DIM3_APS = 1B -> Min 1.7, non-COE (passive ideation only)
      * No 3B in Dim 1 or Dim 2 -> not BIO

    If any of these assertions fail, the M4 decision tree will resolve
    Marcus to the wrong level; fix the failing helper before continuing.
    """
    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    assert ratings[Subdimension.DIM1_ADDICTION_MEDS] == RiskRating.DIM1_AM_C
    assert ratings[Subdimension.DIM3_ACTIVE_PSYCH] == RiskRating.DIM3_APS_1B
    # No 3B anywhere -> BIO will not fire in M4.
    assert ratings[Subdimension.DIM1_WITHDRAWAL] != RiskRating.DIM1_W_3B
    assert ratings[Subdimension.DIM2_PHYSICAL] != RiskRating.DIM2_PH_3B
    # Pregnancy NA for a male patient.
    assert ratings[Subdimension.DIM2_PREGNANCY] == RiskRating.DIM2_PR_NA
