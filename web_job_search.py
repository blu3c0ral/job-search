#!/usr/bin/env python3
"""
Web Job Search Agent — discovers job postings via SerpAPI's Google Jobs engine,
pre-filters deterministically using title blacklist and schedule type, then
evaluates matches against the candidate profile using Claude Opus via
jd_matcher, and stores results in Supabase.

Feeds discovered ATS company slugs back to companies_ats_slugs for future
automated scanning by ats_scanners.py.

Usage:
    python web_job_search.py

Required environment variables:
    PROFILE_YAML              — candidate matching profile content
    SERPAPI_API_KEY            — SerpAPI key (google_jobs engine)
    ANTHROPIC_API_KEY         — Anthropic API key (for Claude)
    SUPABASE_URL              — Supabase project URL
    SUPABASE_SERVICE_ROLE_KEY — Supabase service role key
"""

import anthropic
import hashlib
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from ats_scanners import JobPosting, store_results, supabase
from search_providers import SerpApiProvider, JobSearchResult

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

_AGGREGATORS = {"linkedin", "indeed", "glassdoor", "ziprecruiter", "monster", "dice", "simplyhired"}


# ─── Models ──────────────────────────────────────────────────────────────────


class DiscoveredSlug(BaseModel):
    platform: str
    slug: str


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
    """Load whitelist/blacklist titles and locations from Supabase."""
    logger.info("Loading search configuration from Supabase...")

    rows = supabase.table("job_titles").select("title, type").execute().data
    whitelist_titles = [r["title"] for r in rows if r.get("type") == "Whitelist"]
    blacklist_titles = [r["title"].lower() for r in rows if r.get("type") == "Blacklist"]

    rows = supabase.table("location").select("location, type").execute().data
    locations = [r["location"] for r in rows if r.get("type") == "Whitelist"]

    logger.info(
        f"  {len(whitelist_titles)} whitelist titles, {len(blacklist_titles)} blacklist titles, "
        f"{len(locations)} whitelist locations"
    )
    return {
        "whitelist_titles": whitelist_titles,
        "blacklist_titles": blacklist_titles,
        "locations": locations,
    }


# ─── Rotation Helpers ────────────────────────────────────────────────────────


def fetch_target_titles(n: int = 3) -> list[str]:
    """Fetch n least-recently-queried whitelist titles and update rotation metadata."""
    try:
        rows = (
            supabase.table("job_titles")
            .select("title, query_count")
            .eq("type", "Whitelist")
            .order("last_queried", desc=False, nullsfirst=True)
            .limit(n)
            .execute()
            .data
        )
        if not rows:
            logger.warning("  No whitelist titles found")
            return []
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in rows:
            try:
                supabase.table("job_titles").update(
                    {
                        "last_queried": now_iso,
                        "query_count": (row.get("query_count") or 0) + 1,
                    }
                ).eq("title", row["title"]).execute()
            except Exception as exc:
                logger.warning(f"  Failed to update rotation for title {row['title']!r}: {exc}")
        names = [r["title"] for r in rows]
        logger.info(f"  Target titles for this run: {names}")
        return names
    except Exception as exc:
        logger.warning(f"  Failed to fetch target titles: {exc}")
        return []


def fetch_target_companies(n: int = 3) -> list[str]:
    """Fetch n least-recently-queried target companies and update rotation metadata."""
    try:
        rows = (
            supabase.table("target_companies")
            .select("name, query_count")
            .order("last_queried", desc=False, nullsfirst=True)
            .limit(n)
            .execute()
            .data
        )
        if not rows:
            logger.warning("  No target companies found in DB")
            return []
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in rows:
            try:
                supabase.table("target_companies").update(
                    {
                        "last_queried": now_iso,
                        "query_count": (row.get("query_count") or 0) + 1,
                    }
                ).eq("name", row["name"]).execute()
            except Exception as exc:
                logger.warning(f"  Failed to update rotation metadata for {row['name']}: {exc}")
        names = [r["name"] for r in rows]
        logger.info(f"  Target companies for this run: {names}")
        return names
    except Exception as exc:
        logger.warning(f"  Failed to fetch target companies: {exc}")
        return []


# ─── Query Building ──────────────────────────────────────────────────────────


def build_serpapi_queries(
    titles: list[str],
    companies: list[str],
    location: str = "New York, NY",
) -> list[dict]:
    """Build google_jobs query dicts — no LLM call needed.

    Produces ~7 queries:
      4 x title-based (broad discovery, past week)
      3 x company-targeted (from rotation, past month — wider window)

    Note: The `location` param is SerpAPI's geo-targeting (city-level).
    DB locations ("remote", "us", "nyc") are for post-filtering, not search.
    """
    defaults = ["software engineer", "backend engineer", "machine learning engineer", "staff engineer"]
    title_list = [titles[i] if i < len(titles) else defaults[i] for i in range(4)]
    loc = location

    queries: list[dict] = []

    # 4 title-based broad discovery
    for title in title_list:
        queries.append({"query": title, "location": loc, "chips": "date_posted:week"})

    # 3 company-targeted (wider window — a 3-week-old posting from a target company is still valuable)
    for company in companies[:3]:
        queries.append({
            "query": f"{company} {t1}",
            "location": loc,
            "chips": "date_posted:month",
        })

    logger.info(f"  Built {len(queries)} queries ({len(titles)} titles, {len(companies[:3])} companies)")
    for i, q in enumerate(queries, 1):
        logger.info(f"    {i}. q={q['query']!r}  loc={q['location']!r}  chips={q['chips']!r}")
    return queries


# ─── Pre-filtering ───────────────────────────────────────────────────────────


def _posted_at_to_days(posted_at: str) -> int | None:
    """Parse posted_at string into approximate number of days old.

    Handles: "2 days ago", "1 week ago", "3 hours ago", and ISO dates.
    """
    if not posted_at:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(posted_at[:19], fmt)
            return (date.today() - dt.date()).days
        except ValueError:
            pass
    lower = posted_at.lower()
    try:
        parts = lower.split()
        n = int(parts[0])
        unit = parts[1]
        if "hour" in unit or "minute" in unit:
            return 0
        if "day" in unit:
            return n
        if "week" in unit:
            return n * 7
        if "month" in unit:
            return n * 30
        if "year" in unit:
            return n * 365
    except (IndexError, ValueError):
        pass
    return None


def deduplicate_results(
    results: list[JobSearchResult],
) -> list[JobSearchResult]:
    """Remove duplicate jobs. Dedup on (company, title) pair and apply_option URLs."""
    seen_keys: set[str] = set()
    seen_urls: set[str] = set()
    unique: list[JobSearchResult] = []

    for r in results:
        key = f"{r.company.lower().strip()}|{r.title.lower().strip()}"
        if key in seen_keys:
            continue

        result_urls = {opt.get("link", "").rstrip("/") for opt in r.apply_options if opt.get("link")}
        if result_urls & seen_urls:
            continue

        seen_keys.add(key)
        seen_urls.update(result_urls)
        unique.append(r)

    return unique


def pre_filter_results(
    results: list[JobSearchResult],
    existing_urls: set[str],
    blacklist_titles: list[str],
    max_age_days: int = 30,
) -> list[JobSearchResult]:
    """Deterministic pre-filtering — zero LLM cost.

    Filters:
      1. Blacklist title keywords (intern, junior, manager, ios, etc.)
      2. Schedule type (part-time, contract, internship, temporary)
      3. Already in DB (any apply_option URL matches existing)
      4. Age (posted_at > max_age_days)
      5. Empty/short description
    """
    filtered: list[JobSearchResult] = []
    counts = {"blacklist": 0, "schedule": 0, "existing": 0, "stale": 0, "empty": 0}

    for r in results:
        title_lower = r.title.lower()

        # 1. Blacklist title filter
        if any(bl in title_lower for bl in blacklist_titles):
            counts["blacklist"] += 1
            continue

        # 2. Schedule type filter
        if r.schedule_type and r.schedule_type.lower() in {
            "part-time", "contract", "internship", "temporary",
        }:
            counts["schedule"] += 1
            continue

        # 3. Already in DB (check all apply_option URLs)
        result_urls = {opt.get("link", "").rstrip("/") for opt in r.apply_options if opt.get("link")}
        if result_urls & existing_urls:
            counts["existing"] += 1
            continue

        # 4. Age filter
        if r.posted_at:
            days = _posted_at_to_days(r.posted_at)
            if days is not None and days > max_age_days:
                counts["stale"] += 1
                continue

        # 5. Empty description
        if len(r.description) < 100:
            counts["empty"] += 1
            continue

        filtered.append(r)

    total_dropped = sum(counts.values())
    if total_dropped:
        logger.info(
            f"  Pre-filter: dropped {total_dropped} "
            f"(blacklist={counts['blacklist']}, schedule={counts['schedule']}, "
            f"existing={counts['existing']}, stale={counts['stale']}, empty={counts['empty']})"
        )
    return filtered


# ─── URL / ATS Helpers ───────────────────────────────────────────────────────


def get_existing_urls() -> set[str]:
    """Get URLs already in job_search_main to avoid re-processing."""
    rows = supabase.table("job_search_main").select("link").execute().data
    return {r["link"] for r in rows if r.get("link")}


def extract_ats_slugs(result: JobSearchResult) -> list[DiscoveredSlug]:
    """Parse apply_options links for ATS platform slugs."""
    discovered: list[DiscoveredSlug] = []
    seen: set[str] = set()
    for opt in result.apply_options:
        link = opt.get("link", "")
        for platform, pattern in ATS_URL_PATTERNS.items():
            m = pattern.match(link)
            if m:
                slug = m.group("slug")
                key = f"{platform}|{slug}"
                if key not in seen:
                    discovered.append(DiscoveredSlug(platform=platform, slug=slug))
                    seen.add(key)
                break
    return discovered


def pick_best_apply_url(
    result: JobSearchResult,
) -> tuple[str, str | None, str | None, str | None]:
    """Pick the best URL from apply_options.

    Priority: ATS direct link > company careers page > any available link.
    Returns (url, ats_platform, ats_slug, ats_job_id).
    """
    ats_match = None
    company_link = None

    for opt in result.apply_options:
        link = opt.get("link", "")
        title_lower = (opt.get("title") or "").lower()

        # Check for ATS URL
        for platform, pattern in ATS_URL_PATTERNS.items():
            m = pattern.match(link)
            if m:
                job_id = m.group("job_id")
                if platform == "Lever" and job_id and len(job_id) != 36:
                    job_id = None
                ats_match = (link, platform, m.group("slug"), job_id)
                break
        if ats_match:
            break

        # Non-aggregator link = likely company careers page
        if not company_link and not any(agg in title_lower for agg in _AGGREGATORS):
            company_link = link

    if ats_match:
        return ats_match
    if company_link:
        return (company_link, None, None, None)
    if result.apply_options:
        return (result.apply_options[0].get("link", ""), None, None, None)
    return ("", None, None, None)


# ─── Slug Storage ────────────────────────────────────────────────────────────


def store_discovered_slugs(discovered: list[DiscoveredSlug]) -> dict:
    """Add newly discovered ATS slugs to companies_ats_slugs."""
    if not discovered:
        return {"new_slugs": 0, "updated_slugs": 0}

    logger.info(f"Checking {len(discovered)} discovered ATS slugs...")

    existing = (
        supabase.table("companies_ats_slugs").select("slug, platform").execute().data
    )
    existing_map = {r["slug"]: r.get("platform") or [] for r in existing}

    new_count = 0
    updated_count = 0

    for item in discovered:
        slug = item.slug
        platform = item.platform

        if slug not in existing_map:
            try:
                supabase.table("companies_ats_slugs").insert(
                    {"slug": slug, "platform": [platform]}
                ).execute()
                new_count += 1
                existing_map[slug] = [platform]
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


# ─── Company Auto-Discovery ──────────────────────────────────────────────────


def auto_discover_companies(results: list[JobSearchResult]) -> int:
    """Extract company names from search results and add new ones to target_companies."""
    if not results:
        return 0

    candidates: set[str] = set()
    for r in results:
        name = r.company.strip()
        if name and name.lower() not in {"unknown", "n/a", "", "none", "confidential"}:
            candidates.add(name)

    if not candidates:
        return 0

    try:
        existing_rows = supabase.table("target_companies").select("name").execute().data
        existing_lower = {r["name"].lower() for r in existing_rows}
    except Exception as exc:
        logger.warning(f"  auto_discover_companies: failed to fetch existing: {exc}")
        return 0

    new_count = 0
    for name in sorted(candidates):
        if name.lower() in existing_lower:
            continue
        try:
            supabase.table("target_companies").insert(
                {"name": name, "source": "auto_discovered"}
            ).execute()
            existing_lower.add(name.lower())
            new_count += 1
            logger.info(f"  Auto-discovered company: {name}")
        except Exception as exc:
            logger.warning(f"  Failed to insert auto-discovered company {name!r}: {exc}")

    if new_count:
        logger.info(f"  Auto-discovered {new_count} new companies")
    return new_count


# ─── ID Generation ───────────────────────────────────────────────────────────


def generate_web_job_id(url: str) -> str:
    """Generate deterministic ID from URL. SHA256 hash, first 16 chars."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def run_web_search() -> dict:
    """Main orchestration — 5 phases, zero Sonnet calls.

    1. Load profile, config, existing URLs
    2. Build & execute SerpAPI google_jobs searches
    3. Pre-filter deterministically (blacklist, schedule, dedup, age)
    4. Convert to JobPostings and store (with Opus deep matching)
    5. Discover ATS slugs and companies
    """
    t0 = time.monotonic()

    # ── Phase 1: Load inputs ──────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 1 — Loading profile and config")
    logger.info("=" * 60)
    profile = load_profile()
    config = load_search_config()
    serpapi_key = os.environ.get("SERPAPI_API_KEY")
    if not serpapi_key:
        logger.error("SERPAPI_API_KEY environment variable is not set")
        sys.exit(1)
    provider = SerpApiProvider(serpapi_key)
    client = anthropic.Anthropic()
    existing_urls = get_existing_urls()
    logger.info(f"  {len(existing_urls)} existing URLs in DB")

    # ── Phase 2: Build & execute searches ─────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 2 — SerpAPI google_jobs search")
    logger.info("=" * 60)
    titles = fetch_target_titles(n=4)
    companies = fetch_target_companies(n=3)
    queries = build_serpapi_queries(titles, companies)

    all_results: list[JobSearchResult] = []
    for i, q in enumerate(queries, 1):
        logger.info(f"  [{i}/{len(queries)}] {q['query']} (loc={q['location']}, chips={q['chips']})")
        results = provider.search(q["query"], q["location"], chips=q["chips"], max_pages=1)
        all_results.extend(results)
        logger.info(f"    -> {len(results)} results")
        if i < len(queries):
            time.sleep(1)
    logger.info(f"Total raw results: {len(all_results)}")

    if not all_results:
        logger.info("No results from SerpAPI.")
        return {"total_found": 0, "stored": 0, "slugs_discovered": 0, "queries": [q["query"] for q in queries]}

    # ── Phase 3: Pre-filter ───────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 3 — Pre-filter (deterministic)")
    logger.info("=" * 60)
    unique_results = deduplicate_results(all_results)
    logger.info(f"After dedup: {len(unique_results)} (removed {len(all_results) - len(unique_results)} dupes)")

    filtered_results = pre_filter_results(
        unique_results,
        existing_urls=existing_urls,
        blacklist_titles=config["blacklist_titles"],
        max_age_days=30,
    )
    logger.info(f"After pre-filter: {len(filtered_results)} candidates")

    # Collect ATS slugs from ALL results (even filtered ones — slug discovery is cheap)
    all_slugs: list[DiscoveredSlug] = []
    for r in unique_results:
        all_slugs.extend(extract_ats_slugs(r))

    if not filtered_results:
        logger.info("Nothing new to process after filtering.")
        slug_result = store_discovered_slugs(all_slugs)
        auto_discover_companies(unique_results)
        return {
            "total_found": 0,
            "stored": 0,
            "slugs_discovered": slug_result["new_slugs"],
            "queries": [q["query"] for q in queries],
        }

    # ── Phase 4: Convert to JobPostings and store ─────────────────────────────
    logger.info("=" * 60)
    logger.info(f"PHASE 4 — Store {len(filtered_results)} jobs (with Opus deep matching)")
    logger.info("=" * 60)

    job_postings: list[JobPosting] = []
    for r in filtered_results:
        url, ats_platform, ats_slug, ats_job_id = pick_best_apply_url(r)
        if not url:
            url = r.apply_options[0]["link"] if r.apply_options else ""

        job_id = ats_job_id or generate_web_job_id(url) if url else generate_web_job_id(r.job_id)
        platform = ats_platform or "Web"

        job_postings.append(
            JobPosting(
                id=str(job_id),
                title=r.title,
                company=r.company,
                location=r.location,
                compensation=r.compensation,
                url=url,
                platform=platform,
                matched_keywords=[r.query],
                description=r.description,
                apply_url=url,
            )
        )

        logger.info(
            f"  [{platform}] {r.company} — {r.title} | {r.location} | "
            f"{r.compensation} | posted: {r.posted_at or '?'}"
        )

    storage = store_results(job_postings, profile=profile, anthropic_client=client)

    # ── Phase 5: Discover slugs and companies ─────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 5 — Discover slugs and companies")
    logger.info("=" * 60)
    slug_result = store_discovered_slugs(all_slugs)
    auto_discover_companies(filtered_results)

    elapsed = time.monotonic() - t0
    logger.info(
        f"\nWeb search complete in {elapsed:.1f}s: "
        f"{storage['inserted']} jobs stored, "
        f"{slug_result['new_slugs']} new slugs discovered"
    )

    return {
        "total_found": len(filtered_results),
        "stored": storage["inserted"],
        "slugs_discovered": slug_result["new_slugs"],
        "queries": [q["query"] for q in queries],
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
