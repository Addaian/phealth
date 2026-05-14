#!/usr/bin/env python3
"""
generate_chart_text.py — render Marcus Reyes' chart text from persona.yaml.

Outputs four plain-text files to app/synthetic/chart_text/, each formatted as
field-by-field blocks the clinician can paste into SimplePractice's UI:

    bps_intake.txt    — the [BPS] Intake Assessment, 21 sections
    note_1_soap.txt   — Day 2 SOAP note (Dr. Aisha Patel, MD)
    note_2_dap.txt    — Day 5 DAP note (Jordan Kim, CPRS)
    note_3_dsap.txt   — Day 8 DSAP note (Maria Gonzales, LCSW)

Pipeline role
-------------
    persona.yaml  →  generate_chart_text.py  →  chart_text/*.txt
                                                ↓  (manual paste into SP)
                                                SimplePractice Data Export ZIP
                                                ↓  (M6 ingest pipeline)
                                                ClinicalDocument rows

Why deterministic templates rather than LLM generation
------------------------------------------------------
Golden tests in tests/test_scale_extractor.py assert that extracted
observations match expected substrings (e.g. "PHQ-9 administered 2026-05-04:
anhedonia 3, depressed mood 3, ..."). LLM rephrasing on each run would
silently break those tests. See playbook §"staged decision points" — clinical
realism is achieved by hand-curated templates seeded from persona.yaml, not
on-the-fly generation.

Run
---
From repo root:

    python app/synthetic/generate_chart_text.py

Output files are overwritten on every run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PERSONA_PATH = Path(__file__).parent / "persona.yaml"
OUTPUT_DIR = Path(__file__).parent / "chart_text"

# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────
HR = "─" * 72  # section/field divider (visual only — not pasted into SP)
DHR = "═" * 72  # file-level header divider


def banner(title_block: str) -> str:
    """Render a file-level header block. Humans read this, SP never sees it."""
    return f"{DHR}\n  {title_block}\n{DHR}\n"


def section(num: int, title: str) -> str:
    """Render a section banner — what the clinician matches against SP's section header."""
    return f"\n{HR}\n§{num}. {title}\n{HR}\n"


def field(label: str, body: str) -> str:
    """Render one paste-into-SP block.

    The clinician finds the matching field in SP (by label), then copies
    everything between this block's header and the next '▶'.
    """
    return f"\n▶ {label}\n\n{body.strip()}\n"


def flow(s: str) -> str:
    """Collapse YAML folded-block whitespace ('>' style) into clean prose.

    YAML folded blocks ('>') replace newlines with spaces but preserve
    indentation as multiple spaces — this normalizes to a single space.
    """
    return " ".join(s.split())


# ─────────────────────────────────────────────────────────────────────────────
# BPS Intake renderer
# ─────────────────────────────────────────────────────────────────────────────
def render_bps_intake(p: dict[str, Any]) -> str:
    """Render the [BPS] Intake Assessment as paste-into-SP plain text.

    Produces all 21 sections of the BPS template the user built in M1.
    See documents/phase_1_PRD.md §6.1 for the section contract.
    """
    enc = p["encounter"]
    demo = p["demographics"]
    out = banner(
        f"[BPS] Intake Assessment for {demo['given_name']} {demo['family_name']}\n"
        f"  Date: {enc['bps_intake_date']}\n"
        f"  Clinician: {enc['bps_clinician']['name']}\n"
        f"  Encounter type: Initial admission, BPS intake\n"
        f"  Note: demographics are captured at the SP client-record level,\n"
        f"  not in this form. Skip to §1 when pasting."
    )

    # ── §1 Presenting Problem ────────────────────────────────────────────
    out += section(1, "Presenting Problem")
    out += field("Presenting Problem", p["presenting_problem"]["chief_complaint"].strip())

    # ── §2 Signs and Symptoms ────────────────────────────────────────────
    # DSM-5-TR symptom checklist tied to the three working diagnoses in §20.
    out += section(2, "Signs and Symptoms (DSM-V-TR based)")
    out += field(
        "Signs and Symptoms",
        flow("""
        Patient meets DSM-5-TR criteria for Alcohol Use Disorder, severe
        (≥6 criteria endorsed: tolerance, withdrawal, hazardous use,
        continued use despite social/occupational consequences, time spent
        obtaining/using, unsuccessful cut-down attempts); Sedative-Hypnotic
        Use Disorder, severe (alprazolam); and Major Depressive Disorder,
        recurrent, moderate (anhedonia, depressed mood, sleep disturbance,
        fatigue, appetite change, low self-worth, poor concentration,
        psychomotor change — PHQ-9 = 18, moderately severe). Current
        alcohol withdrawal syndrome with tremor, sweats, anxiety, insomnia
        (CIWA-Ar = 12). Resulting impairment spans: occupational (job loss
        6 wks ago for intoxication at work), social (isolation from former
        sober peers), affective (severe anxiety GAD-7 = 15, depression
        PHQ-9 = 18), physical (witnessed fall, uncontrolled HTN,
        alcohol-related hepatic injury), and cognitive (poor concentration,
        impaired insight/judgment on MSE).
    """),
    )

    # ── §3 History of Presenting Problem ────────────────────────────────
    out += section(3, "History of Presenting Problem")
    out += field("History of Presenting Problem", flow(p["presenting_problem"]["hpi"]))
    out += field(
        "Frequency/duration/severity/cycling of symptoms",
        flow("""
        Alcohol: 4 years of escalating daily use, currently ~14 standard
        drinks/day. Alprazolam: 18 months of daily use, escalated from a
        legitimate 0.5 mg PRN prescription to illicit 1 mg daily. Cannabis:
        recreational, 2–3x weekly. Acute alcohol withdrawal currently
        moderate (CIWA-Ar = 12). Benzodiazepine withdrawal anticipated but
        not yet manifest. Depressive and anxious symptoms have been
        continuously present x 4 years with worsening since job loss 6
        weeks ago.
    """),
    )
    out += field(
        "Was there a clear time when Sx worsened?",
        flow("""
        Yes — substance use and depressive symptoms escalated sharply 6
        weeks ago following job loss (terminated for intoxication at work).
        Acute withdrawal began approximately 36 hours prior to admission
        after a forced cut-down attempt (no alcohol available at home over
        the weekend).
    """),
    )
    out += field(
        "Family mental health history",
        flow(p["family_social_history"]["family_psychiatric_history"]),
    )

    # ── §4 Current Family and Significant Relationships ─────────────────
    out += section(4, "Current Family and Significant Relationships")
    out += field(
        "Strengths/support", "Brother Daniel willing to engage; provided transportation to ED."
    )
    out += field(
        "Stressors/problems",
        flow("""
        Brother is an active substance user (alcohol, cannabis). Patient
        is housing-dependent on the brother. No partner, no children.
        Parents live out of state; minimal contact.
    """),
    )
    out += field(
        "Recent changes",
        "Job loss 6 weeks ago; pending DUI charge. Brother delivered ultimatum about housing.",
    )
    out += field(
        "Changes desired", "Stable sober housing; restored autonomy; reconnect with mother."
    )
    out += field(
        "Comment on family circumstances",
        flow("""
        Father with untreated alcohol use disorder. Mother with treated
        major depression. Patient is the younger of two siblings.
    """),
    )

    # ── §5 Childhood/Adolescent History ──────────────────────────────────
    out += section(5, "Childhood/Adolescent History")
    out += field(
        "Childhood/Adolescent History",
        flow("""
        Normative early development. Raised in two-parent household until
        parental separation at age 11. First alcohol use at age 16 (family
        setting, moderate). First cannabis at age 17 (peer setting). High
        school graduate; completed 18 credits at community college. No
        documented abuse or neglect. 2019 MVA at age 28 with mild TBI —
        brief LOC, evaluated and discharged from ED. No documented
        cognitive sequelae, but patient reports lingering difficulty with
        sustained concentration since.
    """),
    )

    # ── §6 Social Relationships ──────────────────────────────────────────
    out += section(6, "Social Relationships")
    out += field(
        "Strengths/support",
        "Brother Daniel (limited — actively using). "
        "Former AA sponsor from 2023 cycle, lost contact.",
    )
    out += field(
        "Stressors/problems",
        "Social isolation post-job-loss. Drift from former sober peer group. "
        "No current peer-recovery network.",
    )
    out += field(
        "Recent changes",
        "Has not contacted former sponsor in 12+ months. "
        "Sober friends drifted as drinking escalated in 2024.",
    )
    out += field(
        "Changes desired",
        "Reconnect with former sponsor; build new sober peer network through structured group.",
    )

    # ── §7 Cultural/Ethnic ───────────────────────────────────────────────
    out += section(7, "Cultural/Ethnic")
    out += field(
        "Strengths/support",
        "Identifies as Hispanic/Latino. Strong cultural value placed on family ties (familismo).",
    )
    out += field(
        "Stressors/problems",
        "Cultural shame around mental health and substance use. "
        "Internalized 'be a man, handle it yourself' narrative.",
    )
    out += field(
        "Beliefs/practices to incorporate into therapy",
        flow("""
        Family-systems framing welcomed. Spanish-language psychoeducation
        materials acceptable. Open to culturally-aware peer matching.
    """),
    )

    # ── §8 Spiritual/Religious ───────────────────────────────────────────
    out += section(8, "Spiritual/Religious")
    out += field(
        "Strengths/support",
        "Raised Catholic. Faith was a source of comfort in childhood; "
        "mother and grandmother were practicing.",
    )
    out += field(
        "Stressors/problems",
        "Currently disconnected from practice. Reports guilt and shame around "
        "substance use as a barrier to returning to church.",
    )
    out += field(
        "Beliefs/practices to incorporate into therapy",
        flow("""
        Open to facility chaplaincy if available. Open to AA/NA frameworks
        that incorporate higher-power language without requiring it.
    """),
    )
    out += field(
        "Recent changes",
        "Has not attended Mass in 4+ years. "
        "Grandmother (primary spiritual influence) died in 2022.",
    )
    out += field(
        "Changes desired",
        "Wants to 'find some peace' but not actively pursuing a specific practice at this time.",
    )

    # ── §9 Legal ─────────────────────────────────────────────────────────
    out += section(9, "Legal")
    out += field("History", "No prior arrests or convictions.")
    out += field(
        "Status/impact/stressors",
        flow("""
        Pending DUI charge (2026-04-12). Court date TBD; assigned public
        defender. May lose driver's license. Stressor weighing heavily on
        treatment ambivalence and willingness to engage.
    """),
    )

    # ── §10 Education ────────────────────────────────────────────────────
    out += section(10, "Education")
    out += field(
        "Strengths", "High school graduate. Completed 18 credits of community college coursework."
    )
    out += field(
        "Weaknesses",
        flow("""
        Did not complete associate's degree. Reports difficulty with
        sustained concentration since 2019 mTBI, exacerbated by current
        substance use.
    """),
    )

    # ── §11 Employment/Vocational ────────────────────────────────────────
    out += section(11, "Employment/Vocational")
    out += field(
        "Strengths/support",
        "5-year tenure in warehouse logistics (2020–2026). Considered reliable when sober.",
    )
    out += field(
        "Stressors/problems",
        flow("""
        Unemployed x 6 weeks. Terminated for intoxication at work
        (workplace breathalyzer policy violation). Income loss is a major
        stressor. No unemployment income — termination was 'for cause.'
        Identity disruption: "I'm not used to not working."
    """),
    )

    # ── §12 Military ─────────────────────────────────────────────────────
    out += section(12, "Military")
    out += field("Current impact", "No military service history.")

    # ── §13 Leisure/Recreational ─────────────────────────────────────────
    out += section(13, "Leisure/Recreational")
    out += field(
        "Strengths/support",
        "Previously enjoyed watching basketball and walking in the neighborhood.",
    )
    out += field(
        "Recent changes",
        "Has not engaged in leisure activities x 6 months — "
        "substance use has crowded everything else out.",
    )
    out += field(
        "Changes desired",
        "Wants to 'get back into walking' as a wellness practice during/after treatment.",
    )

    # ── §14 Physical Health ──────────────────────────────────────────────
    # Load-bearing for ASAM Dim 2 (Biomedical Conditions).
    med = p["medical_history"]
    v = med["vitals_on_admission"]
    out += section(14, "Physical Health")
    out += field(
        "Summary of health",
        flow("""
        Active: uncontrolled essential hypertension, suspected alcoholic
        hepatitis (not biopsy-confirmed), remote mild TBI (2019). No
        current primary care relationship. Last PCP visit 2024 for
        naltrexone prescription.
    """),
    )
    out += field(
        "Physical factors affecting mental condition",
        flow("""
        Severe sleep deprivation (<2 h/night for several days
        pre-admission). Withdrawal physiology (autonomic arousal, tremor)
        directly affecting cognition, mood, and concentration. Hepatic
        dysfunction may affect medication metabolism — clinical relevance
        for benzo taper and any future SSRI/SNRI selection.
    """),
    )
    out += field(
        "Vitals (BP, HR, RR, temp, SpO2)",
        f"BP {v['blood_pressure']}; HR {v['heart_rate']}; "
        f"RR {v['respiratory_rate']}; T {v['temperature_f']}°F; "
        f"SpO2 {v['oxygen_saturation']}%.",
    )
    out += field(
        "Current medications (name, dose, frequency, prescriber)",
        flow("""
        None at admission. Was prescribed lisinopril 10 mg PO daily by PCP
        in 2024 for HTN; non-adherent x 18 months. To be re-initiated on
        admission day 4 per inpatient medical orders.
    """),
    )
    out += field("Allergies", "No known drug allergies (NKDA).")
    out += field(
        "Recent labs (date, panel, results)",
        flow("""
        Hepatic panel 2026-05-04: AST 142 U/L (ref 10–40), ALT 98 U/L (ref
        7–56), GGT 312 U/L (ref 9–48), total bilirubin 1.4 mg/dL (ref
        0.1–1.2) — consistent with alcohol-related hepatic injury.
        UDS 2026-05-04: POSITIVE for ethanol, benzodiazepines, cannabinoids;
        NEGATIVE for opioids, cocaine, amphetamines.
    """),
    )

    # ── §15 Chemical Use History ────────────────────────────────────────
    # Load-bearing for ASAM Dim 1 (withdrawal) and Dim 4 (use-related risk).
    out += section(15, "Chemical Use History")
    out += field(
        "Summary of use",
        flow("""
        Polysubstance: heavy daily alcohol (4 years), daily illicit
        alprazolam (18 months), recreational cannabis (years). Denies
        opioid, stimulant, and hallucinogen use; UDS corroborates. Active
        alcohol withdrawal at intake (CIWA-Ar = 12); benzodiazepine
        withdrawal anticipated.
    """),
    )
    out += field(
        "Patient's perception of problem",
        flow("""
        Minimizes severity: "I drink a lot but it's not that bad."
        Attributes ED visit to "just a fall, not the drinking."
        Acknowledges alprazolam use is "getting out of hand" but does not
        connect it to the withdrawal-anticipation finding. Ambivalent
        about treatment: "I'm here because my brother said so."
    """),
    )

    # Per-substance breakdown — each substance gets one bullet paragraph.
    subs_lines = []
    for s in p["substance_use"]:
        # Substances with no use (e.g. opioids — denied) render shorter.
        if "DENIES" in str(s.get("current_pattern", "")).upper():
            subs_lines.append(
                f"  • {s['substance']}: {s['current_pattern']}. {s.get('notes', '')}".rstrip()
            )
        else:
            subs_lines.append(
                flow(f"""
                • {s["substance"]}: first use age {s.get("age_of_first_use", "?")};
                pattern {s.get("current_pattern", "?")};
                last use {s.get("last_use", "?")};
                route {s.get("route", "?")};
                withdrawal history — {s.get("withdrawal_history", "none")};
                prior treatment — {s.get("prior_treatment", "none")}.
            """)
            )
    out += field(
        "Per-substance breakdown (alcohol, sedative-hypnotics, cannabis, "
        "opioids, stimulants, other) — for each: first/last use, route, "
        "frequency, amounts, withdrawal hx, prior tx",
        "\n".join(subs_lines),
    )
    out += field(
        "AUDIT-C (date, items, total, interpretation)", flow(p["scales"]["audit_c"]["verbatim"])
    )
    out += field(
        "DAST-10 (date, items, total, interpretation)", flow(p["scales"]["dast10"]["verbatim"])
    )

    # ── §16 Counseling/Prior Treatment History ──────────────────────────
    out += section(16, "Counseling/Prior Treatment History")
    out += field(
        "Summary of prior treatment",
        flow("""
        2023 — Outpatient individual counseling, 6 weeks at community mental
        health center. Self-discontinued. 2024 — PCP-prescribed naltrexone
        50 mg PO daily x 3 months; non-adherent and discontinued. No prior
        residential or inpatient SUD treatment.
    """),
    )
    out += field(
        "Benefits of previous treatment",
        "Brief reduction in drinking (~30% by self-report) during the 2023 counseling cycle.",
    )
    out += field(
        "Setbacks of previous treatment",
        flow("""
        Self-discontinued before completing planned course in 2023.
        Naltrexone non-adherence with no follow-up. No prior MAT, no prior
        recovery group attendance.
    """),
    )

    # ── §17 Past Psychiatric History ─────────────────────────────────────
    psy = p["psychiatric_history"]
    out += section(17, "Past Psychiatric History")
    out += field(
        "Prior psychiatric diagnoses (DSM-5-TR / ICD-10)",
        "\n".join(
            f"  • {d['name']} (ICD-10 {d['icd10']}; "
            f"DSM-5-TR {d['dsm5tr']}; onset {d['onset_year']})"
            for d in psy["diagnoses"]
        ),
    )
    out += field(
        "Psychiatric hospitalizations (year, facility, reason, duration)",
        "\n".join(
            f"  • {h['year']} — {h['facility']} — {h['reason']}" for h in psy["hospitalizations"]
        ),
    )
    out += field(
        "Prior psychiatric medications (drug, dose, dates, prescriber, response)",
        "\n".join(
            f"  • {m['name']} — {m['years']} — {m['outcome']}" for m in psy["prior_medications"]
        ),
    )
    out += field("Self-harm history", flow(psy["self_harm_history"]))
    out += field("Prior suicide attempts (lifetime)", "None.")

    # ── §18 Mental Status Exam ───────────────────────────────────────────
    mse = p["mental_status_exam"]
    out += section(18, "Mental Status Exam")
    for label, key in [
        ("Appearance", "appearance"),
        ("Behavior / psychomotor", "behavior"),
        ("Speech", "speech"),
        ("Mood (client-reported)", "mood"),
        ("Affect (clinician-observed)", "affect"),
        ("Thought process", "thought_process"),
        ("Thought content", "thought_content"),
        ("Perception", "perception"),
        ("Cognition / orientation", "cognition"),
        ("Insight", "insight"),
        ("Judgment", "judgment"),
    ]:
        out += field(label, mse[key])
    out += field(
        "Standardized screening — PHQ-9 (date, items, total)", flow(p["scales"]["phq9"]["verbatim"])
    )
    out += field(
        "Standardized screening — GAD-7 (date, items, total)", flow(p["scales"]["gad7"]["verbatim"])
    )

    # ── §19 Risk Assessment ──────────────────────────────────────────────
    # Critical for gap G3 (C-SSRS re-administration) and Dim 1 (CIWA).
    out += section(19, "Risk Assessment")
    out += field(
        "Suicide risk — C-SSRS (date, items, interpretation)",
        flow(p["scales"]["c_ssrs"]["verbatim"]),
    )
    out += field("Homicidal ideation", "Denies. No ideation, plan, intent, or lifetime history.")
    out += field(
        "Withdrawal risk — CIWA-Ar (date, items, total)", flow(p["scales"]["ciwa_ar"]["verbatim"])
    )
    out += field(
        "Withdrawal risk — COWS (date, items, total, or N/A)", flow(p["scales"]["cows"]["verbatim"])
    )
    out += field(
        "Violence / aggression risk",
        "Low. No history of interpersonal violence; no current threats or ideation.",
    )
    out += field(
        "Overall risk level",
        flow("""
        Moderate — driven by withdrawal severity (CIWA-Ar = 12), passive
        suicidal ideation (C-SSRS Q2-positive), and unstable recovery
        environment.
    """),
    )
    out += field(
        "Safety plan / precautions",
        flow("""
        Inpatient monitoring with CIWA-Ar q4h; nursing line of sight if
        CIWA ≥ 15. Fall precautions in place. Suicide precautions: standard
        observation given passive ideation without plan/intent.
        Re-administer C-SSRS prior to any level-of-care change per
        NPSG.15.01.01.
    """),
    )

    # ── §20 Diagnosis ────────────────────────────────────────────────────
    out += section(20, "Diagnosis")
    out += field(
        "Primary diagnosis (DSM-5-TR / ICD-10)",
        "Alcohol Use Disorder, severe, with withdrawal (F10.232; DSM-5-TR 303.90).",
    )
    out += field(
        "Secondary diagnosis (DSM-5-TR / ICD-10)",
        "Sedative, Hypnotic, or Anxiolytic Use Disorder, severe (F13.20; DSM-5-TR 304.10).",
    )
    out += field(
        "Tertiary diagnosis (DSM-5-TR / ICD-10)",
        "Major Depressive Disorder, recurrent episode, moderate (F33.1; DSM-5-TR 296.32).",
    )
    out += field(
        "Differential / rule-outs",
        flow("""
        Generalized Anxiety Disorder (F41.1) is a co-occurring consideration,
        but anxiety symptoms may be substantially driven by current
        withdrawal physiology — reassess 2 weeks post-stabilization.
        Recommended level of care on admission: ASAM 3.7 — Medically
        Monitored Intensive Inpatient, driven by Dim 1 (active polysubstance
        withdrawal) and Dim 5 (unsafe recovery environment).
    """),
    )

    # ── §21 Treatment Plan ───────────────────────────────────────────────
    # Golden-thread anchor: interventions listed here are what progress
    # notes must reference. G2 = peer-support is intentionally NOT listed.
    tp = p["treatment_plan"]
    out += section(21, "Treatment Plan")
    for i, g in enumerate(tp["goals"], 1):
        body = (
            f"Domain: {g['domain']}. "
            f"Goal: {g['goal']}. "
            f"Target date: {g['target_date']}. "
            f"Measure: {g['measure']}."
        )
        out += field(f"Goal {i} (domain, goal, target date, measure)", body)
    out += field(
        "Interventions (list with frequency)", "\n".join(f"  • {iv}" for iv in tp["interventions"])
    )
    out += field("Expected outcomes", "\n".join(f"  • {o}" for o in tp["expected_outcomes"]))
    out += field(
        "Discharge / step-down criteria",
        flow("""
        Step down to PHP (ASAM 2.5) when: (a) CIWA-Ar < 5 for 48 hours,
        (b) BP controlled on lisinopril, (c) sober housing option
        identified, (d) C-SSRS re-administered and stable. Target step-down
        date: 2026-05-15.
    """),
    )

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Progress-note renderer
# ─────────────────────────────────────────────────────────────────────────────
def render_progress_note(p: dict[str, Any], note_idx: int) -> str:
    """Render one progress note (SOAP, DAP, or DSAP) as paste-into-SP text.

    Sections render in canonical order for the note's format (playbook §F):
        SOAP → subjective, objective, assessment, plan
        DAP  → data, assessment, plan
        DSAP → data, subjective, assessment, plan
    """
    note = p["progress_notes"][note_idx]
    fmt = note["format"]

    out = banner(
        f"[{fmt}] Progress Note for {p['demographics']['given_name']} "
        f"{p['demographics']['family_name']}\n"
        f"  Date: {note['date']}\n"
        f"  Clinician: {note['author']['name']} ({note['author']['role']})\n"
        f"  Encounter type: {note['encounter_type']}\n"
        f"  Template to select in SP: '{note['template_title']}'"
    )

    section_order = {
        "SOAP": ["subjective", "objective", "assessment", "plan"],
        "DAP": ["data", "assessment", "plan"],
        "DSAP": ["data", "subjective", "assessment", "plan"],
    }[fmt]

    for key in section_order:
        label = key.capitalize()
        out += field(label, flow(note["sections"][key]))

    # Author-only footer flagging which intentional gap this note embeds.
    # DO NOT paste this into SimplePractice.
    gap = note.get("embeds_gap")
    if gap:
        gap_detail = next(g for g in p["intentional_gaps"] if g["id"] == gap)
        out += (
            f"\n{HR}\n"
            f"AUTHOR NOTE — intentional gap embedded here: "
            f"{gap} ({gap_detail['ep_code']})\n"
            f"  {gap_detail['gap']}\n"
            f"DO NOT paste this footer into SimplePractice.\n"
            f"{HR}\n"
        )

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    with PERSONA_PATH.open() as f:
        persona = yaml.safe_load(f)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    outputs = {
        "bps_intake.txt": render_bps_intake(persona),
        "note_1_soap.txt": render_progress_note(persona, 0),
        "note_2_dap.txt": render_progress_note(persona, 1),
        "note_3_dsap.txt": render_progress_note(persona, 2),
    }
    for filename, content in outputs.items():
        path = OUTPUT_DIR / filename
        path.write_text(content)
        print(f"  wrote {path}  ({len(content):,} chars)")


if __name__ == "__main__":
    main()
