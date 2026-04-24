#!/usr/bin/env python3
"""
JD Matcher — evaluates job descriptions against a candidate profile using Claude API.

Model: claude-opus-4-6 with adaptive thinking, effort=high.
To downgrade to Sonnet, change model to "claude-sonnet-4-6" and remove the
thinking and output_config parameters from the evaluate_jd() call.

Usage:
    python jd_matcher.py --profile matching_profile.yaml --jds jds.json [--output results.json]

JDs input format (jds.json):
    [
        {
            "title": "Senior Backend Engineer",
            "company": "Anthropic",
            "jd_text": "Full job description text..."
        },
        ...
    ]

Each JD is evaluated independently (no shared context between calls).
"""

import anthropic
import json
import argparse
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import logging
logger = logging.getLogger("jd_matcher")


# ============================================================
# PRE-SCREENING CONSTANTS
# ============================================================

_staffing_agencies_cache: frozenset[str] | None = None


def _get_staffing_agencies() -> frozenset[str]:
    """Load staffing agencies from DB (cached after first call)."""
    global _staffing_agencies_cache
    if _staffing_agencies_cache is None:
        try:
            db = get_supabase_client()
            rows = db.table("staffing_agencies").select("name").execute().data
            _staffing_agencies_cache = frozenset(r["name"].lower().strip() for r in rows)
            logger.debug(f"Loaded {len(_staffing_agencies_cache)} staffing agencies from DB")
        except Exception:
            _staffing_agencies_cache = frozenset()
    return _staffing_agencies_cache

CONTRACT_SIGNALS = [
    "contract to hire",
    "c2h",
    "corp-to-corp",
    "corp to corp",
    "c2c",
    "hourly rate",
    "independent contractor",
    "1099",
    "w2 contract",
    "staffing agency",
    "our client",
    "direct client",
]

CONTRACT_SIGNAL_THRESHOLD = 2

PRESCREEN_SYSTEM_PROMPT = """You are a job pre-screening filter. Your ONLY job is to identify OBVIOUS hard-no mismatches against the candidate profile provided. You must be CONSERVATIVE — when in doubt, answer PASS.

You will receive the candidate's profile and a job posting excerpt. Use the profile's dealbreakers, anti_preferences, technologies, target_roles, compensation_range, and role_type_avoid sections to determine if the job is a clear mismatch.

REJECT ONLY if the job CLEARLY triggers one of these:
1. STAFFING/CONTRACT: The company is obviously a staffing agency, or the role is contract/freelance/C2H — and the profile lists contract roles as an anti-preference or dealbreaker
2. WRONG PRIMARY STACK: The role PRIMARILY requires technologies the candidate has low enthusiasm for (1-2) with NO overlap with their high-enthusiasm technologies
3. BELOW COMP FLOOR: Compensation is EXPLICITLY stated AND the MAX of the range makes it impossible to reach the candidate's compensation floor even with equity/bonus
4. DEALBREAKER TRIGGERED: The role clearly matches something in the candidate's dealbreakers or anti_preferences list (e.g. client-facing, no growth opportunity)
5. ROLE TYPE MISMATCH: The role type is listed in the candidate's role_type_avoid

CRITICAL RULES:
- If the JD is ambiguous on any dimension, PASS it through
- If the JD mentions a low-enthusiasm tech but also mentions high-enthusiasm tech, PASS
- If comp is not stated, PASS (do NOT guess)
- Comp floor tolerance: if max compensation is within 5% of the candidate's floor, PASS — do not reject on rounding
- A role at a reputable company with unclear details should PASS
- NYC metro geography: all 5 boroughs (Manhattan, Brooklyn, Queens, Bronx, Staten Island), plus NJ within commute distance (Hoboken, Jersey City, Newark, etc.) are ALL valid locations — do NOT reject these as "outside NYC metro"
- False positives (rejecting a good job) are 10x worse than false negatives (passing a bad job)

Respond with ONLY valid JSON. Keep the reason under 20 words:
{"decision": "REJECT" or "PASS", "reason": "<20 words max>"}"""

PRESCREEN_USER_TEMPLATE = """<profile>
{profile}
</profile>

Company: {company}
Title: {title}

Job Description (excerpt):
{jd_excerpt}"""


# Lazy Supabase client — only initialized when DB functions are used
_supabase_client = None


def get_supabase_client():
    global _supabase_client
    if _supabase_client is None:
        import os
        from supabase import create_client
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        _supabase_client = create_client(url, key)
    return _supabase_client

# ============================================================
# MATCHING PROMPT (from jd_matching_prompt.md)
# ============================================================

SYSTEM_PROMPT = """
# JD Matching Prompt

You are a JD-matching agent. Your job is to evaluate job descriptions against the attached candidate profile and answer one question: **"Would this person want to apply to this job?"**

This is NOT an ATS match. You are not evaluating whether the candidate would get hired. You are evaluating whether, given everything you know about what excites them, what drains them, and what they need — they would look at this JD and say "yes, I want to apply."

## How to evaluate each JD:

1. **Dealbreaker check** — Does the JD trigger any item in the `dealbreakers` or `anti_preferences` lists? If yes, it's a hard no regardless of other fit. Be specific about which dealbreaker was triggered.

2. **Skill & tech alignment** — Compare the JD's requirements against `core_competencies` and `technologies`. Weight both `depth` and `enthusiasm` — a technology where enthusiasm is 5 but depth is 2 is still a positive signal (they want to grow there). A technology where depth is 4 but enthusiasm is 2 is a weaker match.

3. **Role type & company fit** — Compare against `target_roles`, `target_companies`, and `company_size_preference`. Does this role type match what they're pursuing?

4. **Excitement signal check** — Scan the `interests` section. Does the JD touch any of these areas? If yes, boost the score — these are strong motivational signals that go beyond skills.

5. **Energizer alignment** — Does the day-to-day work described in the JD align with `what_energizes` or `what_drains`? A JD can match on skills perfectly but describe work that drains this person. Read between the lines of the JD — what will Monday morning actually look like?

6. **Logistics** — Location, compensation, remote policy, travel requirements. Check against `geographic_preferences`, `compensation_range`, and `remote_preference`.

7. **Search context** — Check `search_history` for patterns. Given where this person is right now in their career and search, would this be a strategic choice even if it's not the most exciting option?

## Compensation evaluation:

The comp dealbreaker should be evaluated against **total compensation potential** (base + equity + bonus), not the bottom of a posted base salary range. A JD with a base range of $150K–$240K is NOT a dealbreaker — the top of range plus equity could meet the floor. A JD with a range of $130K–$170K IS a dealbreaker — even at the top with equity, it's unlikely to reach the minimum. Flag borderline ranges as a risk to validate early, not as a hard no.

## Read the JD carefully.

Job titles can be misleading. A role titled "Platform Engineer" might be client-facing. A role titled "AI Engineer" might be building internal sales tools. The actual job description — not the title — determines fit.

## Scoring:

Rate each JD on a 1–10 scale where:

- **1–3:** Clear mismatch or dealbreaker triggered
- **4–5:** Some alignment but significant concerns
- **6–7:** Good fit with notable tradeoffs worth naming
- **8–10:** Strong fit, should apply

**Scoring calibration:** The score answers "would this person want to apply?" — NOT "would they get hired?" Skill gaps that affect hiring odds (e.g., a JD asks for 5+ years of Go when the candidate has 3) should be noted in the breakdown but should NOT reduce the score. The score reflects desire and fit. A false negative (skipping a role they'd want) is far more costly than a false positive (applying to one that doesn't work out).

## Output format:

Respond with a JSON object only. No preamble, no markdown, no explanation outside the JSON.

```json
{
    "score": <integer 1-10>,
    "verdict": <"strong apply" | "apply" | "borderline" | "skip" | "hard no">,
    "dealbreaker_triggered": <string describing the dealbreaker, or null if none>,
    "where_it_aligns": [<string>, ...],
    "where_it_breaks_down": [<string>, ...],
    "bottom_line": <1-2 sentence string: apply or skip and why>,
    "comp_risk": <"none" | "low" | "medium" | "high">,
    "comp_note": <string explaining comp assessment, or null>,
    "why_this_company": <string: 1-2 sentences>,
    "why_this_role": <string: 1-2 sentences>,
    "something_i_built_and_proud_of": <string: 1-2 sentences>
}
```

## Application narrative fields:

These three fields will be used verbatim in real job applications. They are NOT internal evaluation notes — they go directly to hiring teams.

CRITICAL: DO NOT invent, embellish, or fabricate any information. Every fact, project name, technology, and claim MUST come directly from the candidate's profile. If the profile doesn't mention it, don't write it. Rephrasing profile content is fine; inventing new content is not.

Write from the candidate's perspective (first person). Keep it simple, confident, and relaxed — no corporate fluff, no buzzwords, no "I'm passionate about", no "I'm excited to". 1-2 sentences each, max.

- **why_this_company**: What draws the candidate to this specific company? Pull from the JD's details about the company's product, mission, or technical challenges. Be specific to this company — generic answers are useless.
- **why_this_role**: Why does this particular role appeal to the candidate given their background? Connect the role's responsibilities to what they actually enjoy doing (from the profile). Only reference skills and interests that appear in the profile.
- **something_i_built_and_proud_of**: Pick the most relevant thing from the candidate's profile that connects to this role's domain. Use only projects, systems, and accomplishments explicitly mentioned in the profile — do not invent or infer projects that aren't there.

## For each JD, provide:

- **Fit score** (1–10)
- **Where it aligns** (be specific — reference profile fields)
- **Where it breaks down or has tension** (be specific and honest)
- **Bottom line** (1–2 sentences: apply or skip, and why)

At the end, provide a **summary ranking table** sorted by fit score.

If you cannot access a URL, search for the role by company name and title, then evaluate based on what you find.
"""


USER_PROMPT_TEMPLATE = """Here is the candidate profile:

<profile>
{profile}
</profile>

Here is the job to evaluate:

Company: {company}
Title: {title}

Job Description:
{jd_text}

Evaluate this JD against the profile and respond with a JSON object only."""


def load_profile(profile_path: str | None) -> str:
    """Load the candidate matching profile from PROFILE_YAML env var or a file."""
    import os
    env_profile = os.environ.get("PROFILE_YAML")
    if env_profile:
        return env_profile
    if profile_path is None:
        print("Error: provide --profile or set PROFILE_YAML env var", file=sys.stderr)
        sys.exit(1)
    path = Path(profile_path)
    if not path.exists():
        print(f"Error: Profile file not found: {profile_path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text()


def load_jds(jds_path: str) -> list[dict]:
    """Load the list of JDs to evaluate."""
    from jd_texts import JD_TEXTS
    path = Path(jds_path)
    jd_texts = JD_TEXTS
    if not path.exists():
        print(f"Error: JDs file not found: {jds_path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        jds = json.load(f)
    if not isinstance(jds, list):
        print("Error: JDs file must contain a JSON array", file=sys.stderr)
        sys.exit(1)
    for i, jd in enumerate(jds):
        for field in ("title", "company", "jd_key"):
            if field not in jd:
                print(
                    f"Error: JD #{i+1} is missing required field '{field}'",
                    file=sys.stderr,
                )
                sys.exit(1)
        jd["jd_text"] = jd_texts[jd["jd_key"]]
    return jds


def evaluate_jd(client: anthropic.Anthropic, profile: str, jd: dict) -> dict:
    """Evaluate a single JD against the profile. Returns parsed result dict."""
    user_message = USER_PROMPT_TEMPLATE.format(
        profile=profile,
        company=jd["company"],
        title=jd["title"],
        jd_text=jd["jd_text"],
    )

    # Retry on 429 rate limit errors with exponential backoff (30s, 60s, 120s)
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=4000,  # enough for thinking + JSON output
                thinking={"type": "adaptive"},  # model decides when/how much to think per task
                output_config={
                    "effort": "medium"  # default, but explicit — deep reasoning for matching decisions
                },
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            break  # success — exit retry loop
        except anthropic.RateLimitError:
            if attempt >= max_retries:
                logger.error(
                    f"evaluate_jd: rate limited after {max_retries} retries for "
                    f"{jd['company']} — {jd['title']}, giving up"
                )
                raise
            wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
            logger.warning(
                f"evaluate_jd: rate limited (attempt {attempt + 1}/{max_retries}), "
                f"waiting {wait}s before retry..."
            )
            time.sleep(wait)

    # With adaptive thinking, response may contain thinking blocks + text blocks.
    # We want only the text block (the JSON output).
    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if not raw:
        raise ValueError("No text block found in response")

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = (
            "\n".join(lines[1:-1])
            if lines[-1].strip() == "```"
            else "\n".join(lines[1:])
        )

    result = json.loads(raw)
    result["company"] = jd["company"]
    result["title"] = jd["title"]
    return result


_VERDICT_TO_ENUM = {
    "strong apply": "Excelent Match",
    "apply": "Good Match",
    "borderline": "Relevant",
    "skip": "Less Relevant",
    "hard no": "Not Relevant",
}


def verdict_to_enum(verdict: str) -> str:
    """Map a jd_matcher verdict to the JobMatchEnum value used in Supabase."""
    return _VERDICT_TO_ENUM.get(verdict.lower(), "Relevant")


_NARRATIVE_KEYS = frozenset({
    "why_this_company",
    "why_this_role",
    "something_i_built_and_proud_of",
})

_EXCLUDE_FROM_DETAIL = frozenset({"company", "title"}) | _NARRATIVE_KEYS


# ============================================================
# PRE-SCREENING FUNCTIONS
# ============================================================


def _build_prescreen_result(reason: str, category: str) -> dict:
    """Build a match result dict for a pre-screen rejection."""
    return {
        "match": "Not Relevant",
        "match_detail": {
            "score": 1,
            "verdict": "hard no",
            "dealbreaker_triggered": reason,
            "where_it_aligns": [],
            "where_it_breaks_down": [reason],
            "bottom_line": f"Pre-screened: {reason}",
            "comp_risk": None,
            "comp_note": None,
            "pre_screen": True,
            "pre_screen_category": category,
        },
    }


def _learn_staffing_agency(company: str) -> None:
    """Add a newly-discovered staffing agency to the DB table (idempotent)."""
    name = company.strip()
    if not name or name.lower() in _get_staffing_agencies():
        return
    try:
        db = get_supabase_client()
        db.table("staffing_agencies").upsert(
            {"name": name}, on_conflict="name"
        ).execute()
        # Bust cache so future calls in this process see the new entry
        global _staffing_agencies_cache
        _staffing_agencies_cache = None
        logger.info(f"  Learned new staffing agency: {name}")
    except Exception as exc:
        logger.warning(f"  Failed to learn staffing agency {name!r}: {exc}")


def deterministic_pre_filter(title: str, company: str, jd_text: str) -> dict | None:
    """Instant rejection for known-bad patterns. No API call.

    Returns None if the job passes, or a rejection dict if it should be skipped.
    """
    company_lower = company.lower().strip()

    for blacklisted in _get_staffing_agencies():
        if blacklisted in company_lower:
            return _build_prescreen_result(
                f"Company '{company}' is a known staffing agency",
                "company_blacklist",
            )

    jd_lower = jd_text.lower()
    hits = sum(1 for signal in CONTRACT_SIGNALS if signal in jd_lower)
    if hits >= CONTRACT_SIGNAL_THRESHOLD:
        _learn_staffing_agency(company)
        return _build_prescreen_result(
            f"JD contains {hits} contract/staffing signals",
            "contract_signals",
        )

    return None


def haiku_pre_screen(
    title: str,
    company: str,
    jd_text: str,
    profile: str,
    anthropic_client: anthropic.Anthropic,
) -> dict | None:
    """Cheap Haiku pre-screen. Returns rejection dict or None (pass through).

    Sends first 2000 chars of JD (covers intro + requirements for most postings). Fail-open on any error.
    """
    jd_excerpt = jd_text[:2000] if jd_text else ""

    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=PRESCREEN_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": PRESCREEN_USER_TEMPLATE.format(
                    profile=profile,
                    company=company,
                    title=title,
                    jd_excerpt=jd_excerpt,
                ),
            }],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

        result = json.loads(raw)

        if result.get("decision", "").upper() == "REJECT":
            reason = result.get("reason", "Haiku pre-screen rejection")
            logger.info(f"  Haiku REJECT: {company} — {title}: {reason}")
            reason_lower = reason.lower()
            if any(kw in reason_lower for kw in ("staffing", "contract role", "staffing agency", "recruiting")):
                _learn_staffing_agency(company)
            return _build_prescreen_result(
                f"Haiku: {reason}",
                "haiku_prescreen",
            )

        logger.debug(f"  Haiku PASS: {company} — {title}")
        return None

    except Exception as exc:
        logger.warning(f"  Haiku pre-screen error for {company} — {title}: {exc}. Passing through.")
        return None


def evaluate_and_store(
    job_id: str,
    platform: str,
    *,
    profile: str | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> dict:
    """
    Fetch a job from Supabase, evaluate it, and write match results back.

    Args:
        job_id:           The `id` column value.
        platform:         The `source_platform` column value.
        profile:          Candidate profile text. Loads from PROFILE_YAML env var if omitted.
        anthropic_client: Reuse an existing Anthropic client, or one is created automatically.

    Returns:
        The full evaluation result dict (same shape as evaluate_jd()).
    """
    db = get_supabase_client()

    logger.info(f"Fetching job id={job_id!r} platform={platform!r} from Supabase...")
    row = (
        db.table("job_search_main")
        .select("id, source_platform, role_title, company, job_description")
        .eq("id", job_id)
        .eq("source_platform", platform)
        .single()
        .execute()
        .data
    )
    if not row:
        raise ValueError(f"Job not found: id={job_id!r} platform={platform!r}")

    logger.info(f"  Found: {row['company']} — {row['role_title']}")
    jd_text = row.get("job_description") or ""
    if not jd_text:
        logger.warning("  job_description is empty — evaluation will be low quality")

    jd = {
        "title": row["role_title"],
        "company": row["company"],
        "jd_text": jd_text,
    }

    if profile is None:
        profile = load_profile(None)

    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    logger.info("  Evaluating with Claude...")
    result = evaluate_jd(anthropic_client, profile, jd)

    match_enum = verdict_to_enum(result.get("verdict", ""))
    match_detail = {k: result[k] for k in result if k not in _EXCLUDE_FROM_DETAIL}

    logger.info(
        f"  Result: {match_enum} (score {result.get('score', '?')}/10, "
        f"verdict: {result.get('verdict', '?')})"
    )
    logger.info("  Writing match + match_detail to Supabase...")
    update_data = {"match": match_enum, "match_detail": match_detail}
    for k in _NARRATIVE_KEYS:
        if k in result:
            update_data[k] = result[k]
    db.table("job_search_main").update(update_data).eq("id", job_id).eq("source_platform", platform).execute()
    logger.info("  Done.")

    return result


def evaluate_match(
    title: str,
    company: str,
    jd_text: str,
    *,
    profile: str | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> dict:
    """
    Evaluate a JD and return match fields ready for DB storage.

    Use this when you already have the JD text (e.g. during scanning pipelines).

    Returns:
        {"match": <JobMatchEnum string>, "match_detail": <dict>}
    """
    if profile is None:
        profile = load_profile(None)
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    logger.debug(f"evaluate_match: {company} — {title} ({len(jd_text)} chars)")

    # Layer 1: Deterministic pre-filter (instant, no API call)
    det_result = deterministic_pre_filter(title, company, jd_text)
    if det_result is not None:
        logger.info(
            f"  DETERMINISTIC REJECT: {company} — {title}: "
            f"{det_result['match_detail']['dealbreaker_triggered']}"
        )
        return det_result

    # Layer 2: Haiku pre-screen (cheap, ~$0.001/job)
    haiku_result = haiku_pre_screen(title, company, jd_text, profile, anthropic_client)
    if haiku_result is not None:
        return haiku_result

    # Layer 3: Opus deep evaluation
    jd = {"title": title, "company": company, "jd_text": jd_text}
    result = evaluate_jd(anthropic_client, profile, jd)

    match_enum = verdict_to_enum(result.get("verdict", ""))
    match_detail = {k: result[k] for k in result if k not in _EXCLUDE_FROM_DETAIL}

    # Learn staffing agencies from Opus dealbreaker analysis
    dealbreaker = (result.get("dealbreaker_triggered") or "").lower()
    if any(kw in dealbreaker for kw in ("staffing", "contract", "recruiting agency")):
        _learn_staffing_agency(company)

    out = {"match": match_enum, "match_detail": match_detail}
    for k in _NARRATIVE_KEYS:
        if k in result:
            out[k] = result[k]

    logger.debug(
        f"evaluate_match result: {match_enum} "
        f"(score {result.get('score', '?')}/10)"
    )
    return out


def verdict_emoji(verdict: str) -> str:
    mapping = {
        "strong apply": "🟢",
        "apply": "🟢",
        "borderline": "🟡",
        "skip": "🔴",
        "hard no": "⛔",
    }
    return mapping.get(verdict.lower(), "⚪")


def comp_risk_label(risk: str) -> str:
    mapping = {
        "none": "",
        "low": " ⚠️ comp: low risk",
        "medium": " ⚠️ comp: validate early",
        "high": " 🚨 comp: likely below floor",
    }
    return mapping.get(risk.lower(), "")


def print_results(results: list[dict]) -> None:
    """Print results in a human-readable format similar to agent output."""
    print("\n" + "=" * 70)
    print("  JD MATCHING RESULTS")
    print("=" * 70)

    # Sort by score descending
    sorted_results = sorted(results, key=lambda r: r.get("score", 0), reverse=True)

    for r in sorted_results:
        score = r.get("score", "?")
        verdict = r.get("verdict", "unknown")
        company = r.get("company", "?")
        title = r.get("title", "?")
        emoji = verdict_emoji(verdict)
        comp = comp_risk_label(r.get("comp_risk", "none"))

        print(f"\n{emoji} {company} — {title}")
        print(f"   Score: {score}/10  |  Verdict: {verdict.upper()}{comp}")

        if r.get("dealbreaker_triggered"):
            print(f"   ⛔ DEALBREAKER: {r['dealbreaker_triggered']}")

        print(f"\n   Where it aligns:")
        for point in r.get("where_it_aligns", []):
            print(f"     • {point}")

        print(f"\n   Where it breaks down:")
        for point in r.get("where_it_breaks_down", []):
            print(f"     • {point}")

        if r.get("comp_note"):
            print(f"\n   Comp note: {r['comp_note']}")

        print(f"\n   Bottom line: {r.get('bottom_line', '')}")
        print()

    # Summary table
    print("=" * 70)
    print("  SUMMARY RANKING")
    print("=" * 70)
    print(f"  {'#':<3} {'Score':<7} {'Verdict':<15} {'Company':<20} {'Role'}")
    print(f"  {'-'*3} {'-'*6} {'-'*14} {'-'*19} {'-'*30}")
    for i, r in enumerate(sorted_results, 1):
        emoji = verdict_emoji(r.get("verdict", ""))
        score = r.get("score", "?")
        verdict = r.get("verdict", "unknown").upper()
        company = r.get("company", "?")[:19]
        title = r.get("title", "?")[:40]
        print(f"  {i:<3} {score:<7} {verdict:<15} {company:<20} {title}")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate JDs against a candidate profile using Claude API"
    )
    parser.add_argument(
        "--profile",
        required=False,
        default=None,
        help="Path to the candidate matching profile (YAML or text). Can also set PROFILE_YAML env var.",
    )
    parser.add_argument(
        "--jds",
        required=False,
        default=None,
        help="Path to JDs JSON file (array of {title, company, jd_text})",
    )
    parser.add_argument(
        "--db-id",
        default=None,
        help="Evaluate a single job from Supabase by its id column value",
    )
    parser.add_argument(
        "--db-platform",
        default=None,
        help="source_platform value to pair with --db-id",
    )
    parser.add_argument(
        "--output",
        default="results.json",
        help="Path for JSON output (default: results.json)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds between API calls (default: 0.5)",
    )
    args = parser.parse_args()

    # --- DB single-job mode ---
    if args.db_id or args.db_platform:
        if not (args.db_id and args.db_platform):
            print("Error: --db-id and --db-platform must be used together", file=sys.stderr)
            sys.exit(1)
        profile = load_profile(args.profile)
        print(f"\nEvaluating id={args.db_id!r} platform={args.db_platform!r} ...")
        try:
            result = evaluate_and_store(args.db_id, args.db_platform, profile=profile)
            print(f"  score: {result.get('score', '?')}/10  verdict: {result.get('verdict', '?')}")
            print(f"  match enum: {verdict_to_enum(result.get('verdict', ''))}")
            print(f"\n  Bottom line: {result.get('bottom_line', '')}")
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # --- File-based batch mode ---
    if not args.jds:
        print("Error: provide --jds <file> or use --db-id / --db-platform", file=sys.stderr)
        sys.exit(1)

    profile = load_profile(args.profile)
    jds = load_jds(args.jds)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

    print(f"\nEvaluating {len(jds)} JD(s)...")
    results = []

    for i, jd in enumerate(jds, 1):
        print(
            f"  [{i}/{len(jds)}] {jd['company']} — {jd['title']}...",
            end=" ",
            flush=True,
        )
        try:
            result = evaluate_jd(client, profile, jd)
            results.append(result)
            print(f"score: {result.get('score', '?')}/10")
        except json.JSONDecodeError as e:
            print(f"ERROR: failed to parse response as JSON — {e}")
            results.append(
                {"company": jd["company"], "title": jd["title"], "error": str(e)}
            )
        except Exception as e:
            print(f"ERROR: {e}")
            results.append(
                {"company": jd["company"], "title": jd["title"], "error": str(e)}
            )

        if i < len(jds):
            time.sleep(args.delay)

    # Save JSON output
    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nJSON results saved to: {output_path}")

    # Print human-readable summary
    valid_results = [r for r in results if "error" not in r]
    if valid_results:
        print_results(valid_results)

    if len(valid_results) < len(results):
        failed = len(results) - len(valid_results)
        print(f"⚠️  {failed} JD(s) failed to evaluate. Check results.json for details.")


if __name__ == "__main__":
    # run with: python jd_matcher.py --profile profile.yaml --jds jds_samples.json --output results.json
    main()
