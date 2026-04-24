"""
Job Board API Scanners — Ashby, Greenhouse, Lever
Scans public job board APIs, filters by title keywords + location rules,
returns structured matches.
"""

import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from supabase import create_client

load_dotenv()


# ─── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("ats_scanners")


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


# ─── Configuration ───────────────────────────────────────────────────────────


class JobPosting(BaseModel):
    id: str
    title: str
    company: str
    location: str
    compensation: Optional[str] = None
    url: str
    platform: str
    matched_keywords: list[str] = Field(default_factory=list)
    description: str = Field(default="")
    apply_url: str = Field(default="")


supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)

# Module-level config — populated by load_config()
_config_loaded = False
KEYWORDS: list[str] = []
EXCLUDE_KEYWORDS: list[str] = []
LOCATION_INCLUDE: list[str] = []
LOCATION_EXCLUDE: list[str] = []
ASHBY_SLUGS: list[str] = []
GREENHOUSE_SLUGS: list[str] = []
LEVER_SLUGS: list[str] = []


# ─── Helpers ─────────────────────────────────────────────────────────────────


def load_config():
    """Fetch filter configuration from Supabase. Safe to call multiple times."""
    global _config_loaded, KEYWORDS, EXCLUDE_KEYWORDS
    global LOCATION_INCLUDE, LOCATION_EXCLUDE
    global ASHBY_SLUGS, GREENHOUSE_SLUGS, LEVER_SLUGS

    if _config_loaded:
        return

    logger.info("Fetching configuration from Supabase...")
    t0 = time.monotonic()

    logger.info("  Loading job title keywords...")
    rows = supabase.table("job_titles").select("title, type").execute().data
    KEYWORDS = [r["title"].lower() for r in rows if r.get("type") == "Whitelist"]
    EXCLUDE_KEYWORDS = [r["title"].lower() for r in rows if r.get("type") == "Blacklist"]
    logger.info(
        f"    {len(KEYWORDS)} whitelist keywords, "
        f"{len(EXCLUDE_KEYWORDS)} blacklist keywords"
    )

    logger.info("  Loading locations...")
    rows = supabase.table("location").select("location, type").execute().data
    LOCATION_INCLUDE = [r["location"].lower() for r in rows if r.get("type") == "Whitelist"]
    LOCATION_EXCLUDE = [r["location"].lower() for r in rows if r.get("type") == "Blacklist"]
    logger.info(
        f"    {len(LOCATION_INCLUDE)} included locations, "
        f"{len(LOCATION_EXCLUDE)} excluded locations"
    )

    logger.info("  Loading company slugs...")
    rows = supabase.table("companies_ats_slugs").select("slug, platform").execute().data
    ASHBY_SLUGS = [r["slug"] for r in rows if not r.get("platform") or "Ashby" in r["platform"]]
    GREENHOUSE_SLUGS = [r["slug"] for r in rows if not r.get("platform") or "Greenhouse" in r["platform"]]
    LEVER_SLUGS = [r["slug"] for r in rows if not r.get("platform") or "Lever" in r["platform"]]
    logger.info(
        f"    {len(ASHBY_SLUGS)} Ashby, {len(GREENHOUSE_SLUGS)} Greenhouse, "
        f"{len(LEVER_SLUGS)} Lever slugs"
    )

    elapsed = time.monotonic() - t0
    logger.info(f"Configuration loaded in {elapsed:.1f}s")

    _config_loaded = True


def _extract_compensation(raw) -> str:
    """Normalize an Ashby compensation field (str, dict, or None) to a string."""
    if raw is None:
        return "Not listed"
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("compensationTierSummary") or str(raw)
    return str(raw)


def _humanize_slug(slug: str) -> str:
    """Convert a URL slug like 'acme-corp' to 'Acme Corp'."""
    return slug.replace("-", " ").replace("_", " ").title()


def _get_with_retry(
    url: str,
    *,
    timeout: int = 5,
    retries: int = 2,
    session: requests.Session | None = None,
    label: str = "",
    **kwargs,
) -> requests.Response:
    """HTTP GET with automatic retry on timeout (exponential backoff)."""
    getter = session.get if session else requests.get
    tag = f" [{label}]" if label else ""
    for attempt in range(retries + 1):
        try:
            return getter(url, timeout=timeout, **kwargs)
        except requests.exceptions.Timeout:
            if attempt < retries:
                wait = 2**attempt
                logger.warning(
                    f"  Timeout{tag} for {url.split('?')[0]}, "
                    f"retrying in {wait}s ({attempt + 1}/{retries + 1})"
                )
                time.sleep(wait)
            else:
                raise


def _strip_html_tags(html: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r"<li[^>]*>", "\n• ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _format_lever_salary(salary_range: dict | None) -> str:
    """Format Lever's salaryRange object into a readable string."""
    if not salary_range:
        return "Not listed"
    lo = salary_range.get("min")
    hi = salary_range.get("max")
    currency = salary_range.get("currency", "")
    interval = (salary_range.get("interval") or "").replace("-", " ")
    if lo and hi:
        return f"${lo:,.0f} – ${hi:,.0f} {currency} {interval}".strip()
    if lo:
        return f"${lo:,.0f}+ {currency} {interval}".strip()
    return "Not listed"


def _build_lever_description(job: dict) -> str:
    """Compose a full plain-text description from Lever's split fields."""
    parts = []

    if desc := job.get("descriptionPlain", ""):
        parts.append(desc)

    for section in job.get("lists") or []:
        title = section.get("text", "")
        content = _strip_html_tags(section.get("content", ""))
        if title or content:
            parts.append(f"{title}\n{content}")

    if additional := job.get("additionalPlain", ""):
        parts.append(additional)

    return "\n\n".join(parts)


def _title_matches(title: str) -> list[str]:
    """Return list of matched keywords found in the title."""
    t = title.lower()
    if any(k in t for k in EXCLUDE_KEYWORDS):
        return []
    return [k for k in KEYWORDS if k in t]


def _location_ok(location: str) -> bool:
    """Return True if location passes include/exclude rules."""
    loc = location.lower()
    if any(excluded in loc for excluded in LOCATION_EXCLUDE):
        return False
    return any(included in loc for included in LOCATION_INCLUDE)


def _job_to_row(job: "JobPosting") -> dict:
    """Map a JobPosting to a job_search_main table row."""
    description = job.description
    if job.platform == "Greenhouse":
        description = _strip_html_tags(description)

    return {
        "id": job.id,
        "source_platform": job.platform,
        "role_title": job.title,
        "company": job.company,
        "location": job.location,
        "compensation": job.compensation or "Not listed",
        "link": job.url or None,
        "apply_url": job.apply_url or None,
        "search_term_match": ", ".join(job.matched_keywords),
        "date_found": date.today().isoformat(),
        "status": "New",
        "job_description": description,
    }


# ─── Resume Tailoring ───────────────────────────────────────────────────────

TAILOR_QUALIFYING_MATCHES = {"Excelent Match", "Good Match"}
TAILOR_STORAGE_BUCKET = "tailored-resumes"
TAILOR_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _download_base_resume() -> Path | None:
    """
    Download the base resume from Supabase Storage to a local temp file.
    Uses RESUME_STORAGE_PATH env var for the path within the bucket.
    Returns the local Path, or None if not configured / download fails.
    """
    storage_path = os.environ.get("RESUME_STORAGE_PATH", "")
    if not storage_path:
        return None
    try:
        data = supabase.storage.from_(TAILOR_STORAGE_BUCKET).download(storage_path)
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        tmp.write(data)
        tmp.close()
        return Path(tmp.name)
    except Exception as exc:
        logger.warning(f"  Failed to download base resume from storage: {exc}")
        return None


def _tailor_resumes_for_rows(
    rows: list[dict],
    jobs: list["JobPosting"],
    profile: str | None,
    anthropic_client,
) -> None:
    """
    For rows that qualify (Excelent Match / Good Match), tailor the resume
    to the JD and upload to Supabase Storage. Sets ``tailored_resume`` on
    the row dict.

    Skipped silently if RESUME_TAILOR_CONFIG is not set or base resume
    is not available — this makes the feature opt-in.
    """
    config_raw = os.environ.get("RESUME_TAILOR_CONFIG", "")
    if not config_raw:
        return  # tailoring not configured

    resume_path = _download_base_resume()
    if resume_path is None:
        logger.warning("  Resume tailoring: base resume not available, skipping.")
        return

    qualifying = [
        (row, job)
        for row, job in zip(rows, jobs)
        if row.get("match") in TAILOR_QUALIFYING_MATCHES
    ]
    if not qualifying:
        resume_path.unlink(missing_ok=True)
        return

    import json as _json
    from resume_tailoring import tailor_resume_bytes

    tailor_config = _json.loads(config_raw)

    logger.info(
        f"  Tailoring resumes for {len(qualifying)} qualifying matches..."
    )
    t_tailor = time.monotonic()
    tailor_ok = 0

    resume_name = tailor_config.get("resume_name", "Resume")

    for row, job in qualifying:
        company = re.sub(r'[^\w\s-]', '', job.company).strip()
        filename = f"{resume_name} - {company}.docx"
        storage_path = f"{row['source_platform']}/{filename}"
        try:
            docx_bytes, changes, gaps = tailor_resume_bytes(
                str(resume_path),
                job.description,
                profile=profile,
                config=tailor_config,
                anthropic_client=anthropic_client,
            )
            supabase.storage.from_(TAILOR_STORAGE_BUCKET).upload(
                storage_path,
                docx_bytes,
                {"content-type": TAILOR_CONTENT_TYPE},
            )
            row["tailored_resume"] = storage_path
            row["tailoring_changes"] = {"changes": changes, "gaps": gaps}
            tailor_ok += 1
            logger.info(
                f"    {job.company} — {job.title} "
                f"-> resume tailored & uploaded "
                f"({len(changes)} changes, {len(gaps)} gaps)"
            )
        except Exception as exc:
            logger.warning(
                f"    {job.company} — {job.title} -> tailoring failed: {exc}"
            )

    resume_path.unlink(missing_ok=True)

    logger.info(
        f"  Resume tailoring complete: {tailor_ok}/{len(qualifying)} succeeded "
        f"({time.monotonic() - t_tailor:.1f}s)"
    )


def store_results(
    matches: list["JobPosting"],
    *,
    profile: str | None = None,
    anthropic_client=None,
) -> dict:
    """
    Store job matches to the Supabase job_search_main table.
    Deduplicates by (id, source_platform) — repeated runs are safe.

    When ``profile`` is provided, each job is evaluated against the candidate
    profile via jd_matcher and the ``match`` / ``match_detail`` columns are
    populated on insert.

    Returns:
        dict with inserted and skipped counts.
    """
    logger.info("Storing results to Supabase...")
    t0 = time.monotonic()

    if not matches:
        logger.info("  Nothing to insert.")
        return {"inserted": 0, "skipped": 0}

    logger.info("  Loading existing job keys for deduplication...")
    existing = (
        supabase.table("job_search_main")
        .select("id, source_platform")
        .execute()
        .data
    )
    existing_keys: set[tuple[str, str]] = {
        (r["source_platform"], r["id"]) for r in existing
    }
    logger.info(f"    {len(existing_keys)} existing jobs found")

    new_jobs = [m for m in matches if (m.platform, m.id) not in existing_keys]
    skipped = len(matches) - len(new_jobs)
    logger.info(f"    {len(new_jobs)} new jobs to insert, {skipped} duplicates skipped")

    if not new_jobs:
        logger.info("  Nothing to insert.")
        return {"inserted": 0, "skipped": skipped}

    # ── Drop staffing agencies and bad geo before processing ────────────
    from web_job_search import load_staffing_agencies, is_acceptable_location
    agencies = load_staffing_agencies()
    pre_count = len(new_jobs)
    filtered_jobs = []
    staffing_dropped = 0
    geo_dropped = 0
    for j in new_jobs:
        if j.company.lower().strip() in agencies:
            staffing_dropped += 1
            continue
        if not is_acceptable_location(j.location):
            geo_dropped += 1
            continue
        filtered_jobs.append(j)
    if staffing_dropped or geo_dropped:
        logger.info(
            f"    Hard-filter: dropped {staffing_dropped + geo_dropped} "
            f"(staffing={staffing_dropped}, geo={geo_dropped})"
        )
    new_jobs = filtered_jobs

    if not new_jobs:
        logger.info("  Nothing to insert after filtering.")
        return {"inserted": 0, "skipped": skipped}

    rows = [_job_to_row(j) for j in new_jobs]

    # Evaluate match quality when profile is available
    if profile is not None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from jd_matcher import evaluate_match

        if anthropic_client is None:
            import anthropic
            anthropic_client = anthropic.Anthropic()

        logger.info(
            f"  Evaluating {len(rows)} jobs against candidate profile "
            f"(parallel, max_workers=3)..."
        )
        t_eval = time.monotonic()
        eval_ok = 0
        eval_failed = 0

        def _evaluate_one(row, job):
            match_data = evaluate_match(
                job.title, job.company, job.description,
                profile=profile, anthropic_client=anthropic_client,
            )
            row.update(match_data)
            return match_data

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_evaluate_one, row, job): (row, job)
                for row, job in zip(rows, new_jobs)
            }
            for future in as_completed(futures):
                row, job = futures[future]
                try:
                    match_data = future.result()
                    eval_ok += 1
                    detail = match_data.get("match_detail", {})
                    if detail.get("pre_screen"):
                        category = detail.get("pre_screen_category", "pre_screen")
                        label = {
                            "company_blacklist": "BLACKLIST",
                            "contract_signals": "CONTRACT",
                            "haiku_prescreen": "HAIKU",
                        }.get(category, category.upper())
                        logger.info(
                            f"    [{eval_ok + eval_failed}/{len(rows)}] "
                            f"{job.company} — {job.title} "
                            f"-> Not Relevant [{label}]"
                        )
                    else:
                        logger.info(
                            f"    [{eval_ok + eval_failed}/{len(rows)}] "
                            f"{job.company} — {job.title} "
                            f"-> {match_data['match']} "
                            f"(score {detail.get('score', '?')}/10)"
                        )
                except Exception as exc:
                    eval_failed += 1
                    logger.warning(
                        f"    [{eval_ok + eval_failed}/{len(rows)}] "
                        f"{job.company} — {job.title} "
                        f"-> Match evaluation failed: {exc}"
                    )

        logger.info(
            f"  Match evaluation complete: {eval_ok} succeeded, "
            f"{eval_failed} failed ({time.monotonic() - t_eval:.1f}s)"
        )

    inserted = 0
    batch_size = 50
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            supabase.table("job_search_main").insert(batch).execute()
            inserted += len(batch)
            logger.info(f"    Inserted {min(i + batch_size, len(rows))}/{len(rows)} jobs...")
        except Exception as exc:
            logger.error(f"  Batch insert failed (rows {i}–{i + len(batch)}): {exc}")
            # fall back to row-by-row so one bad row doesn't block the rest
            for row in batch:
                try:
                    supabase.table("job_search_main").insert(row).execute()
                    inserted += 1
                except Exception as row_exc:
                    logger.error(
                        f"  Failed to insert '{row.get('role_title')}' "
                        f"({row.get('source_platform')}/{row.get('id')}): {row_exc}"
                    )

    elapsed = time.monotonic() - t0
    logger.info(
        f"Storage complete: {inserted} inserted, {skipped} duplicates skipped "
        f"({elapsed:.1f}s)"
    )

    return {"inserted": inserted, "skipped": skipped}


# ─── Scanners ────────────────────────────────────────────────────────────────


def scan_ashby(slugs: Optional[list[str]] = None, timeout: int = 5) -> dict:
    """
    Scan Ashby job boards for all given company slugs.

    Args:
        slugs:   List of company slugs to scan. Defaults to ASHBY_SLUGS.
        timeout: Request timeout in seconds per slug.

    Returns:
        dict with keys:
            total    — number of matching jobs found
            matches  — list of JobPosting instances
            errors   — list of slugs that failed
    """
    load_config()
    slugs = slugs or ASHBY_SLUGS
    matches: list[JobPosting] = []
    errors = []

    logger.info(f"Scanning Ashby for {len(slugs)} slugs...")
    t0 = time.monotonic()

    for slug in slugs:
        try:
            resp = _get_with_retry(
                f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                timeout=timeout,
                label=slug,
            )
            if resp.status_code != 200:
                logger.debug(
                    f"  Ashby slug '{slug}': HTTP {resp.status_code}, skipping"
                )
                continue
            data = resp.json()
            company_name = _humanize_slug(slug)
            jobs = data.get("jobs", [])
            logger.debug(
                f"  Ashby slug '{slug}' ({company_name}): {len(jobs)} jobs listed"
            )

            slug_matches = 0
            for job in jobs:
                title = job.get("title", "")
                location = job.get("location", "")
                matched_kw = _title_matches(title)
                if not matched_kw:
                    continue
                if not _location_ok(location):
                    continue

                slug_matches += 1
                matches.append(
                    JobPosting(
                        id=job.get("id"),
                        title=title,
                        company=company_name,
                        location=location,
                        compensation=_extract_compensation(job.get("compensation")),
                        url=job.get("jobUrl")
                        or job.get("hostedUrl")
                        or f"https://jobs.ashbyhq.com/{slug}",
                        platform="Ashby",
                        matched_keywords=matched_kw,
                        description=job.get("descriptionPlain", ""),
                        apply_url=job.get("applyUrl", ""),
                    )
                )

            if slug_matches:
                logger.info(
                    f"  {company_name}: {slug_matches} matches "
                    f"out of {len(jobs)} jobs"
                )

        except Exception as exc:
            logger.error(f"Ashby error for slug '{slug}': {exc}")
            errors.append(slug)

    elapsed = time.monotonic() - t0
    logger.info(
        f"Ashby scan complete: {len(matches)} matches, "
        f"{len(errors)} errors ({elapsed:.1f}s)"
    )

    return {"total": len(matches), "matches": matches, "errors": errors}


def scan_greenhouse(slugs: Optional[list[str]] = None, timeout: int = 5) -> dict:
    """
    Scan Greenhouse job boards for all given company slugs.

    Uses ?content=true to fetch job descriptions in the listing call,
    and resolves the company name from the board endpoint once per slug
    (only when matches are found).

    Args:
        slugs:   List of company slugs to scan. Defaults to GREENHOUSE_SLUGS.
        timeout: Request timeout in seconds per slug.

    Returns:
        dict with keys:
            total    — number of matching jobs found
            matches  — list of JobPosting instances
            errors   — list of slugs that failed
    """
    load_config()
    slugs = slugs or GREENHOUSE_SLUGS
    matches: list[JobPosting] = []
    errors = []

    logger.info(f"Scanning Greenhouse for {len(slugs)} slugs...")
    t0 = time.monotonic()

    for slug in slugs:
        try:
            resp = _get_with_retry(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                timeout=timeout,
                label=slug,
                params={"content": "true"},
            )
            if resp.status_code != 200:
                logger.debug(
                    f"  Greenhouse slug '{slug}': HTTP {resp.status_code}, skipping"
                )
                continue
            data = resp.json()
            jobs = data.get("jobs", [])
            logger.debug(f"  Greenhouse slug '{slug}': {len(jobs)} jobs listed")

            company_name = None
            slug_matches = 0

            for job in jobs:
                title = job.get("title", "")
                location = (job.get("location") or {}).get("name", "")
                matched_kw = _title_matches(title)
                if not matched_kw:
                    continue
                if not _location_ok(location):
                    continue

                if company_name is None:
                    board_resp = _get_with_retry(
                        f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                        timeout=timeout,
                        label=slug,
                    )
                    if board_resp.status_code == 200:
                        company_name = board_resp.json().get("name", slug)
                    else:
                        company_name = slug

                slug_matches += 1
                job_url = job.get("absolute_url", "")
                matches.append(
                    JobPosting(
                        id=str(job.get("id")),
                        title=title,
                        company=company_name,
                        location=location,
                        compensation="Not listed",
                        url=job_url,
                        platform="Greenhouse",
                        matched_keywords=matched_kw,
                        description=job.get("content", ""),
                        apply_url=job_url,
                    )
                )

            if slug_matches:
                logger.info(
                    f"  {company_name}: {slug_matches} matches "
                    f"out of {len(jobs)} jobs"
                )

        except Exception as exc:
            logger.error(f"Greenhouse error for slug '{slug}': {exc}")
            errors.append(slug)

    elapsed = time.monotonic() - t0
    logger.info(
        f"Greenhouse scan complete: {len(matches)} matches, "
        f"{len(errors)} errors ({elapsed:.1f}s)"
    )

    return {"total": len(matches), "matches": matches, "errors": errors}


def scan_lever(slugs: Optional[list[str]] = None, timeout: int = 15) -> dict:
    """
    Scan Lever job boards for all given company slugs.

    Uses a persistent HTTP session for TCP/TLS connection reuse — Lever's API
    is notably slower than Ashby/Greenhouse and benefits from pooling.

    Args:
        slugs:   List of company slugs to scan. Defaults to LEVER_SLUGS.
        timeout: Request timeout in seconds per slug (default 15 for Lever).

    Returns:
        dict with keys:
            total    — number of matching jobs found
            matches  — list of JobPosting instances
            errors   — list of slugs that failed
    """
    load_config()
    slugs = slugs or LEVER_SLUGS
    matches: list[JobPosting] = []
    errors = []

    logger.info(f"Scanning Lever for {len(slugs)} slugs...")
    t0 = time.monotonic()

    session = requests.Session()

    for slug in slugs:
        try:
            resp = _get_with_retry(
                f"https://api.lever.co/v0/postings/{slug}",
                timeout=timeout,
                session=session,
                label=slug,
            )
            if resp.status_code != 200:
                logger.debug(
                    f"  Lever slug '{slug}': HTTP {resp.status_code}, skipping"
                )
                continue
            jobs = resp.json()
            if not isinstance(jobs, list):
                logger.warning(
                    f"  Lever slug '{slug}': expected list, "
                    f"got {type(jobs).__name__}, skipping"
                )
                continue

            logger.debug(f"  Lever slug '{slug}': {len(jobs)} jobs listed")
            company_name = _humanize_slug(slug)
            slug_matches = 0

            for job in jobs:
                title = job.get("text", "")
                categories = job.get("categories") or {}
                location = categories.get("location", "")
                matched_kw = _title_matches(title)
                if not matched_kw:
                    continue
                if not _location_ok(location):
                    continue

                slug_matches += 1
                matches.append(
                    JobPosting(
                        id=job.get("id"),
                        title=title,
                        company=company_name,
                        location=location,
                        compensation=_format_lever_salary(job.get("salaryRange")),
                        url=job.get("hostedUrl", ""),
                        platform="Lever",
                        matched_keywords=matched_kw,
                        description=_build_lever_description(job),
                        apply_url=job.get("applyUrl", ""),
                    )
                )

            if slug_matches:
                logger.info(
                    f"  {company_name}: {slug_matches} matches "
                    f"out of {len(jobs)} jobs"
                )

        except Exception as exc:
            logger.error(f"Lever error for slug '{slug}': {exc}")
            errors.append(slug)

    elapsed = time.monotonic() - t0
    logger.info(
        f"Lever scan complete: {len(matches)} matches, "
        f"{len(errors)} errors ({elapsed:.1f}s)"
    )

    return {"total": len(matches), "matches": matches, "errors": errors}


# ─── Run All ─────────────────────────────────────────────────────────────────


def scan_all() -> dict:
    """
    Run all three scanners and merge results into a single output.

    Returns:
        dict with keys:
            total       — combined match count
            matches     — combined list of JobPosting instances
            errors      — dict of platform -> list of failed slugs
            by_platform — per-platform totals
    """
    load_config()

    logger.info("Starting scan across all platforms...")
    t0 = time.monotonic()

    ashby = scan_ashby()
    greenhouse = scan_greenhouse()
    lever = scan_lever()

    all_matches = ashby["matches"] + greenhouse["matches"] + lever["matches"]

    elapsed = time.monotonic() - t0
    logger.info(
        f"All scans complete: {len(all_matches)} total matches in {elapsed:.1f}s "
        f"(Ashby: {ashby['total']}, Greenhouse: {greenhouse['total']}, "
        f"Lever: {lever['total']})"
    )

    all_errors = {
        "ashby": ashby["errors"],
        "greenhouse": greenhouse["errors"],
        "lever": lever["errors"],
    }
    error_count = sum(len(e) for e in all_errors.values())
    if error_count:
        logger.warning(f"Total errors across platforms: {error_count}")

    return {
        "total": len(all_matches),
        "matches": all_matches,
        "errors": all_errors,
        "by_platform": {
            "ashby": ashby["total"],
            "greenhouse": greenhouse["total"],
            "lever": lever["total"],
        },
    }


# ─── CLI Entry Point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = scan_all()

    print(f"\n{'='*50}")
    print(f"Total matches: {results['total']}")
    for platform, count in results["by_platform"].items():
        print(f"  {platform}: {count}")
    if any(results["errors"].values()):
        print(f"Errors: {results['errors']}")

    for platform in ("Ashby", "Greenhouse", "Lever"):
        sample = next(
            (m for m in results["matches"] if m.platform == platform), None
        )
        if sample is None:
            continue
        print(f"\n{'='*50}")
        print(f"Sample {platform} job:")
        for key, val in sample.model_dump().items():
            if key == "description":
                print(f"  {key}: ({len(val)} chars)")
            else:
                print(f"  {key}: {val}")

    profile = os.environ.get("PROFILE_YAML")
    if not profile and Path("profile.yaml").exists():
        profile = Path("profile.yaml").read_text()
        logger.info("Profile loaded from profile.yaml (local fallback)")
    storage = store_results(results["matches"], profile=profile)
    print(f"\n{'='*50}")
    print(
        f"Supabase storage: {storage['inserted']} inserted, "
        f"{storage['skipped']} duplicates skipped"
    )
