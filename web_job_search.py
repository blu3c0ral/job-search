#!/usr/bin/env python3
"""
Web Job Search Agent — discovers job postings via Brave web search,
evaluates them against the candidate profile using Claude, and stores
matches in Supabase. Feeds discovered ATS company slugs back to
companies_ats_slugs for future automated scanning by ats_scanners.py.

Usage:
    python web_job_search.py

Required environment variables:
    PROFILE_YAML              — candidate matching profile content
    BRAVE_SEARCH_API_KEY      — Brave Search API key
    ANTHROPIC_API_KEY         — Anthropic API key (for Claude)
    SUPABASE_URL              — Supabase project URL
    SUPABASE_SERVICE_ROLE_KEY — Supabase service role key
"""

import anthropic
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from ats_scanners import (
    JobPosting,
    store_results,
    _get_with_retry,
    _strip_html_tags,
    _humanize_slug,
    _extract_compensation,
    _format_lever_salary,
    _build_lever_description,
    supabase,
)

load_dotenv()


# ─── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("web_job_search")


def _configure_logging():
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


_configure_logging()


# ─── Constants ───────────────────────────────────────────────────────────────

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
JINA_READER_URL = "https://r.jina.ai/{url}"
CLAUDE_MODEL = "claude-sonnet-4-6"

ATS_URL_PATTERNS = {
    "Ashby": re.compile(
        r"https?://jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9._-]+)(?:/(?P<job_id>[A-Za-z0-9._-]+))?"
    ),
    "Greenhouse": re.compile(
        r"https?://boards\.greenhouse\.io/(?P<slug>[A-Za-z0-9._-]+)(?:/jobs/(?P<job_id>\d+))?"
    ),
    "Lever": re.compile(
        r"https?://jobs\.lever\.co/(?P<slug>[A-Za-z0-9._-]+)(?:/(?P<job_id>[a-f0-9-]+))?"
    ),
}

ATS_API_URLS = {
    "Ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "Greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "Lever": "https://api.lever.co/v0/postings/{slug}",
}


# ─── Models ──────────────────────────────────────────────────────────────────


class BraveSearchResult(BaseModel):
    title: str
    url: str
    description: str  # snippet from Brave
    query: str  # which search query found this


class ClassifiedUrl(BaseModel):
    url: str
    title: str
    snippet: str
    query: str
    ats_platform: str | None = None
    ats_slug: str | None = None
    ats_job_id: str | None = None


# ─── Config & Profile ────────────────────────────────────────────────────────


def load_profile() -> str:
    """Load candidate profile from PROFILE_YAML env var or profile.yaml file."""
    profile = os.environ.get("PROFILE_YAML")
    if profile:
        logger.info("Profile loaded from PROFILE_YAML env var")
        return profile
    path = Path("profile.yaml")
    if path.exists():
        logger.info(f"Profile loaded from {path.resolve()}")
        return path.read_text()
    logger.error("No profile found: set PROFILE_YAML env var or provide profile.yaml")
    sys.exit(1)


def load_search_config() -> dict:
    """Load whitelist titles and locations from Supabase."""
    logger.info("Loading search configuration from Supabase...")

    rows = supabase.table("job_titles").select("title, type").execute().data
    titles = [r["title"] for r in rows if r.get("type") == "Whitelist"]

    rows = supabase.table("location").select("location, type").execute().data
    locations = [r["location"] for r in rows if r.get("type") == "Whitelist"]

    logger.info(
        f"  {len(titles)} whitelist titles, {len(locations)} whitelist locations"
    )
    return {"titles": titles, "locations": locations}


# ─── Brave Search ────────────────────────────────────────────────────────────


def brave_search(
    query: str, api_key: str, count: int = 10
) -> list[BraveSearchResult]:
    """Execute a single Brave Web Search API call."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        resp = requests.get(
            BRAVE_SEARCH_URL,
            headers=headers,
            params={"q": query, "count": count},
            timeout=10,
        )
        if resp.status_code == 429:
            logger.warning(f"  Brave rate limited, waiting 60s...")
            time.sleep(60)
            resp = requests.get(
                BRAVE_SEARCH_URL,
                headers=headers,
                params={"q": query, "count": count},
                timeout=10,
            )
        if resp.status_code != 200:
            logger.warning(
                f"  Brave search failed (HTTP {resp.status_code}) for: {query[:60]}"
            )
            return []
        data = resp.json()
        web_results = data.get("web", {}).get("results", [])
        return [
            BraveSearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                description=r.get("description", ""),
                query=query,
            )
            for r in web_results
            if r.get("url")
        ]
    except Exception as exc:
        logger.error(f"  Brave search error: {exc}")
        return []


# ─── URL Classification ─────────────────────────────────────────────────────


def classify_url(result: BraveSearchResult) -> ClassifiedUrl:
    """Classify a URL as ATS or non-ATS and extract slug/job_id if ATS."""
    classified = ClassifiedUrl(
        url=result.url,
        title=result.title,
        snippet=result.description,
        query=result.query,
    )
    for platform, pattern in ATS_URL_PATTERNS.items():
        match = pattern.match(result.url)
        if match:
            classified.ats_platform = platform
            classified.ats_slug = match.group("slug")
            job_id = match.group("job_id")
            # Lever job IDs are UUIDs (36 chars with dashes)
            if platform == "Lever" and job_id and len(job_id) != 36:
                job_id = None
            classified.ats_job_id = job_id
            break
    return classified


# ─── Description Fetching ────────────────────────────────────────────────────


def fetch_ats_description(
    platform: str, slug: str, job_id: str | None
) -> dict | None:
    """Fetch job data from ATS public API. Returns dict with job fields or None."""
    logger.info(f"    [ATS API] {platform}/{slug} (job_id={job_id})")
    try:
        if platform == "Ashby":
            resp = _get_with_retry(
                ATS_API_URLS["Ashby"].format(slug=slug),
                timeout=10,
                label=f"ashby/{slug}",
            )
            if resp.status_code != 200:
                return None
            jobs = resp.json().get("jobs", [])
            job = None
            if job_id:
                job = next((j for j in jobs if j.get("id") == job_id), None)
                if not job:
                    logger.info(f"    [ATS API] Job {job_id} not found in Ashby/{slug} — likely expired")
                    return None
            elif jobs:
                job = jobs[0]  # no job_id in URL — take first as best guess
            if not job:
                return None
            return {
                "id": job.get("id", ""),
                "title": job.get("title", ""),
                "company": _humanize_slug(slug),
                "location": job.get("location", ""),
                "compensation": _extract_compensation(job.get("compensation")),
                "url": job.get("jobUrl")
                or job.get("hostedUrl")
                or f"https://jobs.ashbyhq.com/{slug}",
                "description": job.get("descriptionPlain", ""),
                "apply_url": job.get("applyUrl", ""),
            }

        elif platform == "Greenhouse":
            resp = _get_with_retry(
                ATS_API_URLS["Greenhouse"].format(slug=slug),
                timeout=10,
                label=f"greenhouse/{slug}",
                params={"content": "true"},
            )
            if resp.status_code != 200:
                return None
            jobs = resp.json().get("jobs", [])
            job = None
            if job_id:
                job = next(
                    (j for j in jobs if str(j.get("id")) == str(job_id)), None
                )
                if not job:
                    logger.info(f"    [ATS API] Job {job_id} not found in Greenhouse/{slug} — likely expired")
                    return None
            elif jobs:
                job = jobs[0]  # no job_id in URL — take first as best guess
            if not job:
                return None
            # Resolve company name
            company = slug
            board_resp = _get_with_retry(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                timeout=10,
                label=f"greenhouse-board/{slug}",
            )
            if board_resp.status_code == 200:
                company = board_resp.json().get("name", slug)
            location = (job.get("location") or {}).get("name", "")
            return {
                "id": str(job.get("id", "")),
                "title": job.get("title", ""),
                "company": company,
                "location": location,
                "compensation": "Not listed",
                "url": job.get("absolute_url", ""),
                "description": _strip_html_tags(job.get("content", "")),
                "apply_url": job.get("absolute_url", ""),
            }

        elif platform == "Lever":
            resp = _get_with_retry(
                ATS_API_URLS["Lever"].format(slug=slug),
                timeout=15,
                label=f"lever/{slug}",
            )
            if resp.status_code != 200:
                return None
            jobs = resp.json()
            if not isinstance(jobs, list):
                return None
            job = None
            if job_id:
                job = next((j for j in jobs if j.get("id") == job_id), None)
                if not job:
                    logger.info(f"    [ATS API] Job {job_id} not found in Lever/{slug} — likely expired")
                    return None
            elif jobs:
                job = jobs[0]  # no job_id in URL — take first as best guess
            if not job:
                return None
            return {
                "id": job.get("id", ""),
                "title": job.get("text", ""),
                "company": _humanize_slug(slug),
                "location": (job.get("categories") or {}).get("location", ""),
                "compensation": _format_lever_salary(job.get("salaryRange")),
                "url": job.get("hostedUrl", ""),
                "description": _build_lever_description(job),
                "apply_url": job.get("applyUrl", ""),
            }

    except Exception as exc:
        logger.warning(f"  ATS fetch failed for {platform}/{slug}: {exc}")
        return None

    return None


def fetch_jina_description(url: str) -> str | None:
    """Fetch rendered page content via Jina Reader API. Returns clean text."""
    logger.info(f"    [Jina Reader] {url[:80]}")
    try:
        resp = requests.get(
            JINA_READER_URL.format(url=url),
            headers={"Accept": "text/plain"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"    [Jina Reader] HTTP {resp.status_code}")
            return None
        text = resp.text.strip()
        if len(text) < 100:
            logger.warning(f"    [Jina Reader] Too little content ({len(text)} chars)")
            return None
        logger.info(f"    [Jina Reader] OK — {len(text):,} chars")
        return text[:20000]
    except Exception as exc:
        logger.warning(f"    [Jina Reader] Failed: {exc}")
        return None


def fetch_description(classified: ClassifiedUrl) -> tuple[str, dict]:
    """
    Fetch job description via best available method.
    Returns (description_text, metadata_dict).
    Tries: ATS API → Jina Reader → Brave snippet fallback.
    """
    metadata = {}

    # ATS URL — try API first
    if classified.ats_platform and classified.ats_slug:
        ats_data = fetch_ats_description(
            classified.ats_platform,
            classified.ats_slug,
            classified.ats_job_id,
        )
        if ats_data:
            desc = ats_data.pop("description", "")
            metadata = ats_data
            if desc:
                logger.info(f"    [Source] ATS API — {len(desc):,} chars")
                return desc, metadata
            logger.info("    [Source] ATS API returned metadata but no description, trying Jina")

    # Jina Reader for any URL
    jina_text = fetch_jina_description(classified.url)
    if jina_text:
        logger.info(f"    [Source] Jina Reader — {len(jina_text):,} chars")
        return jina_text, metadata

    # Fallback to Brave snippet
    logger.warning(f"    [Source] Brave snippet fallback — {len(classified.snippet):,} chars")
    return classified.snippet, metadata


# ─── Claude API ──────────────────────────────────────────────────────────────


def _parse_json_response(text: str):
    """Parse JSON from Claude response, stripping markdown fences if present."""
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = (
            "\n".join(lines[1:-1])
            if lines[-1].strip() == "```"
            else "\n".join(lines[1:])
        )
    return json.loads(raw)


def generate_search_queries(
    client: anthropic.Anthropic,
    profile: str,
    titles: list[str],
    locations: list[str],
) -> list[str]:
    """Use Claude to generate diverse web search queries."""
    logger.info("Generating search queries with Claude...")

    current_year = date.today().year
    current_month = date.today().strftime("%B %Y")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=f"""You are a job search assistant. Generate web search queries to find RECENTLY POSTED job openings.

Today is {current_month}. It is critical that queries are designed to surface fresh, active listings.

Rules:
- Generate exactly 7 search queries
- Each query should find SPECIFIC, RECENTLY PUBLISHED job postings (not career advice or articles)
- Bias heavily toward recency: include "{current_year}" in most queries, and consider terms like "now hiring", "open role", "actively hiring"
- Mix approaches:
  - 2 queries targeting ATS platforms: include "site:jobs.ashbyhq.com" OR "site:boards.greenhouse.io" OR "site:jobs.lever.co"
  - 3 general queries combining job titles with locations or "remote"
  - 2 queries targeting specific industries or interests from the profile
- Use natural search language
- Vary the job title phrasing across queries for diversity

Respond with a JSON array of 7 query strings. Nothing else.""",
        messages=[
            {
                "role": "user",
                "content": f"""Candidate profile:
{profile}

Target job titles (from tracking system):
{chr(10).join(f"- {t}" for t in titles[:30])}

Target locations:
{chr(10).join(f"- {l}" for l in locations)}

Generate 7 diverse search queries to find job postings for this candidate.""",
            }
        ],
    )

    raw = response.content[0].text
    queries = _parse_json_response(raw)
    logger.info(f"  Generated {len(queries)} queries")
    for i, q in enumerate(queries, 1):
        logger.info(f"    {i}. {q}")
    return queries


def triage_search_results(
    client: anthropic.Anthropic,
    profile: str,
    results: list[BraveSearchResult],
) -> list[dict]:
    """Use Claude to filter search results to likely job postings."""
    logger.info(f"Triaging {len(results)} search results with Claude...")

    numbered = "\n".join(
        f"{i}. Title: {r.title} | URL: {r.url} | Snippet: {r.description[:200]}"
        for i, r in enumerate(results)
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=f"""You are a job posting classifier. Given web search results, identify which are actual, specific job postings.

Include:
- Direct links to specific job postings on ATS platforms (Ashby, Greenhouse, Lever, Workday, etc.)
- Direct links to specific roles on company career pages
- Specific job listings on aggregators (LinkedIn, Indeed) IF they link to a distinct role

Exclude:
- Blog posts, articles, hiring news
- Generic "careers" landing pages without a specific role
- Search result pages from aggregators (e.g., indeed.com/jobs?q=...)
- Duplicate listings (same job at same company — keep the most direct link)

Prefer direct company/ATS URLs over aggregator URLs when the same job appears in both.

Consider relevance to this candidate:
{profile[:2000]}

Respond with a JSON array of objects: {{"index": <int>, "url": "<str>", "title": "<inferred job title>", "company": "<inferred company or Unknown>", "reasoning": "<one sentence>"}}

Select the top 15-20 most promising results. If fewer look real, return only real ones.""",
        messages=[
            {
                "role": "user",
                "content": f"Here are {len(results)} search results to classify:\n\n{numbered}\n\nSelect actual job postings relevant to the candidate.",
            }
        ],
    )

    raw = response.content[0].text
    triaged = _parse_json_response(raw)
    logger.info(f"  Triaged to {len(triaged)} likely job postings")
    return triaged


def evaluate_job_relevance(
    client: anthropic.Anthropic,
    profile: str,
    url: str,
    query: str,
    description: str,
) -> dict | None:
    """Quick relevance screening. Returns evaluation dict or None on failure."""
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system="""You are a job relevance evaluator. Given a candidate profile and a job description, determine if this job is worth tracking.

Quick screening — not a deep analysis. Answer:
1. Is this actually a job posting (not a template, error page, or expired listing)?
2. Does it roughly match the candidate's skill set and experience level?
3. Would the candidate plausibly want to apply?

Also extract structured metadata from the description.

Respond with a JSON object:
{
  "is_real_posting": true/false,
  "relevant": true/false,
  "score": <1-5, where 1=clearly irrelevant, 5=strong match>,
  "reason": "<one sentence>",
  "extracted_title": "<job title>",
  "extracted_company": "<company name>",
  "extracted_location": "<location or Remote>",
  "extracted_compensation": "<compensation if mentioned, else Not listed>"
}""",
            messages=[
                {
                    "role": "user",
                    "content": f"""Candidate profile:
{profile}

Job found via search query: "{query}"
URL: {url}

Job content:
{description[:4000]}

Evaluate relevance and extract metadata.""",
                }
            ],
        )
        raw = response.content[0].text
        return _parse_json_response(raw)
    except Exception as exc:
        logger.warning(f"  Evaluation failed for {url[:60]}: {exc}")
        return None


# ─── Deduplication ───────────────────────────────────────────────────────────


def deduplicate_results(
    results: list[BraveSearchResult],
) -> list[BraveSearchResult]:
    """Remove duplicate URLs. Keep first occurrence."""
    seen = set()
    unique = []
    for r in results:
        normalized = r.url.rstrip("/").split("?utm_")[0].split("?ref=")[0]
        if normalized not in seen:
            seen.add(normalized)
            unique.append(r)
    return unique


def get_existing_urls() -> set[str]:
    """Get URLs already in job_search_main to avoid re-processing."""
    rows = supabase.table("job_search_main").select("link").execute().data
    return {r["link"] for r in rows if r.get("link")}


# ─── Slug Storage ────────────────────────────────────────────────────────────


def store_discovered_slugs(discovered: list[ClassifiedUrl]) -> dict:
    """Add newly discovered ATS slugs to companies_ats_slugs."""
    ats_items = [c for c in discovered if c.ats_platform and c.ats_slug]
    if not ats_items:
        return {"new_slugs": 0, "updated_slugs": 0}

    logger.info(f"Checking {len(ats_items)} discovered ATS slugs...")

    existing = (
        supabase.table("companies_ats_slugs").select("slug, platform").execute().data
    )
    existing_map = {r["slug"]: r.get("platform") or [] for r in existing}

    new_count = 0
    updated_count = 0

    for item in ats_items:
        slug = item.ats_slug
        platform = item.ats_platform

        if slug not in existing_map:
            try:
                supabase.table("companies_ats_slugs").insert(
                    {"slug": slug, "platform": [platform]}
                ).execute()
                new_count += 1
                logger.info(f"  Added new slug: {slug} ({platform})")
            except Exception as exc:
                logger.warning(f"  Failed to insert slug {slug}: {exc}")
        elif platform not in existing_map[slug]:
            try:
                updated_platforms = existing_map[slug] + [platform]
                (
                    supabase.table("companies_ats_slugs")
                    .update({"platform": updated_platforms})
                    .eq("slug", slug)
                    .execute()
                )
                updated_count += 1
                logger.info(f"  Updated slug {slug}: added {platform}")
            except Exception as exc:
                logger.warning(f"  Failed to update slug {slug}: {exc}")

    logger.info(f"  Slugs: {new_count} new, {updated_count} updated")
    return {"new_slugs": new_count, "updated_slugs": updated_count}


# ─── ID Generation ───────────────────────────────────────────────────────────


def generate_web_job_id(url: str) -> str:
    """Generate deterministic ID from URL. SHA256 hash, first 16 chars."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def run_web_search() -> dict:
    """Main orchestration function."""
    t0 = time.monotonic()

    # ── Phase 1: Load inputs ──────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 1 — Loading profile and config")
    logger.info("=" * 60)
    profile = load_profile()
    config = load_search_config()
    brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not brave_api_key:
        logger.error("BRAVE_SEARCH_API_KEY environment variable is not set")
        sys.exit(1)
    client = anthropic.Anthropic()

    # ── Phase 2: Generate search queries ─────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 2 — Generating search queries")
    logger.info("=" * 60)
    queries = generate_search_queries(
        client, profile, config["titles"], config["locations"]
    )

    # ── Phase 3: Execute Brave searches ──────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"PHASE 3 — Brave search ({len(queries)} queries)")
    logger.info("=" * 60)
    all_results: list[BraveSearchResult] = []
    for i, query in enumerate(queries, 1):
        logger.info(f"  [{i}/{len(queries)}] {query}")
        results = brave_search(query, brave_api_key)
        all_results.extend(results)
        logger.info(f"    -> {len(results)} results")
        if i < len(queries):
            time.sleep(1.5)
    logger.info(f"Total raw results: {len(all_results)}")

    # ── Phase 4: Deduplicate & filter ─────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 4 — Deduplication and filtering")
    logger.info("=" * 60)
    unique_results = deduplicate_results(all_results)
    logger.info(f"After deduplication: {len(unique_results)} (removed {len(all_results) - len(unique_results)} dupes)")

    existing_urls = get_existing_urls()
    new_results = [r for r in unique_results if r.url not in existing_urls]
    logger.info(
        f"After filtering existing: {len(new_results)} new "
        f"({len(unique_results) - len(new_results)} already in Supabase)"
    )

    if not new_results:
        logger.info("Nothing new to process.")
        return {"total_found": 0, "stored": 0, "slugs_discovered": 0, "queries": queries}

    # ── Phase 5: Claude triage ────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"PHASE 5 — Claude triage ({len(new_results)} candidates)")
    logger.info("=" * 60)
    triaged = triage_search_results(client, profile, new_results)

    if not triaged:
        logger.info("No results passed triage.")
        return {"total_found": 0, "stored": 0, "slugs_discovered": 0, "queries": queries}

    triaged_results = []
    for t in triaged:
        idx = t.get("index")
        if idx is not None and 0 <= idx < len(new_results):
            triaged_results.append(new_results[idx])
        else:
            logger.warning(f"  Triage returned out-of-range index: {idx}")

    logger.info(f"Triaged to {len(triaged_results)} candidates:")
    for r in triaged_results:
        classified_preview = classify_url(r)
        ats_tag = f" [{classified_preview.ats_platform}]" if classified_preview.ats_platform else " [Web]"
        logger.info(f"  {ats_tag} {r.title[:60]} — {r.url[:70]}")

    # ── Phase 6: Classify URLs ────────────────────────────────────────────────
    classified = [classify_url(r) for r in triaged_results]

    # ── Phase 7: Fetch descriptions and evaluate ──────────────────────────────
    logger.info("=" * 60)
    logger.info(f"PHASE 6 — Fetch descriptions & evaluate ({len(classified)} jobs)")
    logger.info("=" * 60)
    evaluated = []
    for i, c in enumerate(classified, 1):
        logger.info(f"  [{i}/{len(classified)}] {c.title[:60]}")
        logger.info(f"    URL: {c.url[:80]}")

        description, metadata = fetch_description(c)
        if not description or len(description) < 50:
            logger.info("    -> Skipped (no useful description)")
            continue

        if not c.ats_platform:
            time.sleep(3)

        evaluation = evaluate_job_relevance(
            client, profile, c.url, c.query, description
        )
        if not evaluation:
            logger.warning("    -> Evaluation failed, skipping")
            continue
        if not evaluation.get("is_real_posting", False):
            logger.info("    -> Not a real posting, skipping")
            continue
        if not evaluation.get("relevant", False):
            logger.info(
                f"    -> Not relevant (score {evaluation.get('score', '?')}/5): "
                f"{evaluation.get('reason', '')}"
            )
            continue

        evaluated.append(
            {
                "classified": c,
                "description": description,
                "metadata": metadata,
                "evaluation": evaluation,
            }
        )
        logger.info(
            f"    -> MATCH score={evaluation.get('score', '?')}/5 | "
            f"{evaluation.get('extracted_company', '?')} — "
            f"{evaluation.get('extracted_title', '?')} | "
            f"{evaluation.get('extracted_location', '?')} | "
            f"comp: {evaluation.get('extracted_compensation', '?')}"
        )

    logger.info(f"Relevant jobs found: {len(evaluated)}")

    if not evaluated:
        logger.info("No relevant jobs found.")
        # Still store any discovered slugs
        all_classified = classified
        slug_result = store_discovered_slugs(all_classified)
        return {
            "total_found": 0,
            "stored": 0,
            "slugs_discovered": slug_result["new_slugs"],
            "queries": queries,
        }

    # ── Phase 8: Store results ────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 7 — Store results")
    logger.info("=" * 60)

    evaluated.sort(key=lambda x: x["evaluation"].get("score", 0), reverse=True)
    top_jobs = evaluated[:10]
    logger.info(f"Top {len(top_jobs)} jobs to store (by score):")
    for item in top_jobs:
        ev = item["evaluation"]
        logger.info(
            f"  score={ev.get('score')}/5 | {ev.get('extracted_company')} — "
            f"{ev.get('extracted_title')} | {ev.get('extracted_location')}"
        )

    # Convert to JobPosting models
    job_postings = []
    for item in top_jobs:
        c = item["classified"]
        meta = item["metadata"]
        ev = item["evaluation"]

        # Prefer ATS metadata over Claude extraction
        job_id = meta.get("id") or generate_web_job_id(c.url)
        platform = c.ats_platform or "Web"
        title = meta.get("title") or ev.get("extracted_title", c.title)
        company = meta.get("company") or ev.get("extracted_company", "Unknown")
        location = meta.get("location") or ev.get("extracted_location", "")
        compensation = meta.get("compensation") or ev.get(
            "extracted_compensation", "Not listed"
        )
        url = meta.get("url") or c.url
        apply_url = meta.get("apply_url") or c.url

        job_postings.append(
            JobPosting(
                id=str(job_id),
                title=title,
                company=company,
                location=location,
                compensation=compensation,
                url=url,
                platform=platform,
                matched_keywords=[c.query],  # stored as search_term_match
                description=item["description"],
                apply_url=apply_url,
            )
        )

    # 11. Store jobs
    storage = store_results(job_postings)

    # 12. Store discovered slugs (from all classified, not just top jobs)
    all_classified = [item["classified"] for item in evaluated]
    slug_result = store_discovered_slugs(all_classified)

    elapsed = time.monotonic() - t0
    logger.info(
        f"\nWeb search complete in {elapsed:.1f}s: "
        f"{storage['inserted']} jobs stored, "
        f"{slug_result['new_slugs']} new slugs discovered"
    )

    return {
        "total_found": len(evaluated),
        "stored": storage["inserted"],
        "slugs_discovered": slug_result["new_slugs"],
        "queries": queries,
    }


# ─── CLI Entry Point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = run_web_search()
    print(f"\n{'='*50}")
    print(f"Web Job Search Results:")
    print(f"  Jobs found:    {result['total_found']}")
    print(f"  Jobs stored:   {result['stored']}")
    print(f"  New ATS slugs: {result['slugs_discovered']}")
    print(f"{'='*50}")
    print(f"\nSearch queries used:")
    for i, q in enumerate(result.get("queries", []), 1):
        print(f"  {i}. {q}")
    print()
