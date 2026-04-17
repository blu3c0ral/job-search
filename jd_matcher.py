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
from jd_texts import JD_TEXTS

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
    "comp_note": <string explaining comp assessment, or null>
}
```

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


def load_profile(profile_path: str) -> str:
    """Load the candidate matching profile."""
    path = Path(profile_path)
    if not path.exists():
        print(f"Error: Profile file not found: {profile_path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text()


def load_jds(jds_path: str) -> list[dict]:
    """Load the list of JDs to evaluate."""
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
        required=True,
        help="Path to the candidate matching profile (YAML or text)",
    )
    parser.add_argument(
        "--jds",
        required=True,
        help="Path to JDs JSON file (array of {title, company, jd_text})",
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
