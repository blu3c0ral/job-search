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
from typing import Optional

import pandas as pd
import requests
from pydantic import BaseModel, Field

from notion_client import Client


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


JD_ID = os.environ["NOTION_JD_ID"]
LOCATIONS_ID = os.environ["NOTION_LOCATIONS_ID"]
SLUGS_ID = os.environ["NOTION_SLUGS_ID"]
JOB_TITLES_ID = os.environ["NOTION_JOB_TITLES_ID"]

notion = Client(auth=os.environ["NOTION_TOKEN"])

# Module-level config — populated by load_config()
_config_loaded = False
KEYWORDS: list[str] = []
EXCLUDE_KEYWORDS: list[str] = []
LOCATION_INCLUDE: list[str] = []
LOCATION_EXCLUDE: list[str] = []
SLUGS: list[str] = []
JD: pd.DataFrame = pd.DataFrame()


# ─── Helpers ─────────────────────────────────────────────────────────────────


def universal_parser(page) -> dict:
    """
    A generic parser for any Notion page.
    It iterates through properties and extracts values based on their type.
    """
    output = {"id": page["id"]}
    props = page.get("properties", {})

    for name, data in props.items():
        p_type = data.get("type")
        content = data.get(p_type)

        if p_type in ["title", "rich_text"]:
            output[name] = (
                "".join([t["plain_text"] for t in content]) if content else ""
            )
        elif p_type in ["select", "status"]:
            output[name] = content["name"] if content else None
        elif p_type in ["url", "number", "checkbox", "email", "phone_number"]:
            output[name] = content
        elif p_type == "multi_select":
            output[name] = [item["name"] for item in content]
        elif p_type == "date":
            output[name] = content["start"] if content else None

    return output


def fetch_notion_table(db_id) -> pd.DataFrame:
    response = notion.databases.retrieve(database_id=db_id)
    data_source_id = response.get("data_sources")[0].get("id")

    all_results = []
    has_more = True
    next_cursor = None

    while has_more:
        db = notion.data_sources.query(
            data_source_id=data_source_id,
            start_cursor=next_cursor if next_cursor else None,
        )
        batch_results = db.get("results", [])
        all_results.extend(batch_results)
        has_more = db.get("has_more", False)
        next_cursor = db.get("next_cursor")

    return pd.DataFrame([universal_parser(page) for page in all_results])


def load_config():
    """Fetch filter configuration from Notion. Safe to call multiple times."""
    global _config_loaded, KEYWORDS, EXCLUDE_KEYWORDS
    global LOCATION_INCLUDE, LOCATION_EXCLUDE, SLUGS, JD

    if _config_loaded:
        return

    logger.info("Fetching configuration from Notion...")
    t0 = time.monotonic()

    logger.info("  Loading job title keywords...")
    keywords_dict = fetch_notion_table(JOB_TITLES_ID).to_dict(orient="records")
    KEYWORDS = [
        kw.get("Title", "").lower()
        for kw in keywords_dict
        if kw.get("Type") == "Whitelist"
    ]
    EXCLUDE_KEYWORDS = [
        kw.get("Title", "").lower()
        for kw in keywords_dict
        if kw.get("Type") == "Blacklist"
    ]
    logger.info(
        f"    {len(KEYWORDS)} whitelist keywords, "
        f"{len(EXCLUDE_KEYWORDS)} blacklist keywords"
    )

    logger.info("  Loading locations...")
    locations_dict = fetch_notion_table(LOCATIONS_ID).to_dict(orient="records")
    LOCATION_INCLUDE = [
        loc.get("Location", "").lower()
        for loc in locations_dict
        if loc.get("Type") == "Whitelist"
    ]
    LOCATION_EXCLUDE = [
        loc.get("Location", "").lower()
        for loc in locations_dict
        if loc.get("Type") == "Blacklist"
    ]
    logger.info(
        f"    {len(LOCATION_INCLUDE)} included locations, "
        f"{len(LOCATION_EXCLUDE)} excluded locations"
    )

    logger.info("  Loading company slugs...")
    SLUGS = fetch_notion_table(SLUGS_ID)["Slug"].tolist()
    logger.info(f"    {len(SLUGS)} slugs")

    logger.info("  Loading job descriptions...")
    JD = fetch_notion_table(JD_ID)
    logger.info(f"    {len(JD)} job descriptions")

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


NOTION_RICH_TEXT_LIMIT = 2000


def _description_to_blocks(description: str, is_html: bool = False) -> list[dict]:
    """Convert a description into Notion paragraph blocks (max 2000 chars each)."""
    if is_html:
        description = _strip_html_tags(description)

    if not description.strip():
        return []

    paragraphs = description.split("\n")
    blocks: list[dict] = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            if current_chunk and len(current_chunk) < NOTION_RICH_TEXT_LIMIT:
                current_chunk += "\n"
            continue

        candidate = f"{current_chunk}\n{para}".strip() if current_chunk else para

        if len(candidate) <= NOTION_RICH_TEXT_LIMIT:
            current_chunk = candidate
        else:
            if current_chunk:
                blocks.append(_make_paragraph_block(current_chunk))
            while len(para) > NOTION_RICH_TEXT_LIMIT:
                blocks.append(_make_paragraph_block(para[:NOTION_RICH_TEXT_LIMIT]))
                para = para[NOTION_RICH_TEXT_LIMIT:]
            current_chunk = para

    if current_chunk.strip():
        blocks.append(_make_paragraph_block(current_chunk))

    return blocks


def _make_paragraph_block(text: str) -> dict:
    """Create a single Notion paragraph block."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def _job_to_notion_properties(job: "JobPosting") -> dict:
    """Map a JobPosting to Notion page properties."""
    return {
        "Role Title": {"title": [{"text": {"content": job.title}}]},
        "Company": {"rich_text": [{"text": {"content": job.company}}]},
        "Location": {"rich_text": [{"text": {"content": job.location}}]},
        "Compensation": {
            "rich_text": [{"text": {"content": job.compensation or "Not listed"}}]
        },
        "Link": {"url": job.url or None},
        "Apply URL": {"url": job.apply_url or None},
        "Source Platform": {"rich_text": [{"text": {"content": job.platform}}]},
        "Search Term Match": {
            "rich_text": [{"text": {"content": ", ".join(job.matched_keywords)}}]
        },
        "ID": {"rich_text": [{"text": {"content": job.id}}]},
        "Date Found": {"date": {"start": date.today().isoformat()}},
        "Status": {"rich_text": [{"text": {"content": "New"}}]},
    }


def _ensure_apply_url_column():
    """Add 'Apply URL' column to JD database if it doesn't already exist."""
    ds_id = (
        notion.databases.retrieve(database_id=JD_ID)
        .get("data_sources", [{}])[0]
        .get("id")
    )
    ds_schema = notion.data_sources.retrieve(data_source_id=ds_id)
    existing_props = ds_schema.get("properties", {})

    if "Apply URL" in existing_props:
        return

    logger.info("  Adding 'Apply URL' column to JD database...")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={"Apply URL": {"url": {}}},
    )


def store_results(matches: list["JobPosting"]) -> dict:
    """
    Store job matches to the Notion JD database.
    Deduplicates by (Source Platform, ID) so repeated runs are safe.

    Returns:
        dict with inserted and skipped counts.
    """
    logger.info("Storing results to Notion JD database...")
    t0 = time.monotonic()

    _ensure_apply_url_column()

    logger.info("  Loading existing jobs for deduplication...")
    existing_df = fetch_notion_table(JD_ID)
    existing_keys: set[tuple[str, str]] = set()
    if not existing_df.empty and "Source Platform" in existing_df.columns and "ID" in existing_df.columns:
        for _, row in existing_df.iterrows():
            platform = str(row.get("Source Platform", "")).strip()
            job_id = str(row.get("ID", "")).strip()
            if platform and job_id:
                existing_keys.add((platform, job_id))
    logger.info(f"    {len(existing_keys)} existing jobs found")

    new_jobs = [
        m for m in matches if (m.platform, m.id) not in existing_keys
    ]
    skipped = len(matches) - len(new_jobs)
    logger.info(
        f"    {len(new_jobs)} new jobs to insert, {skipped} duplicates skipped"
    )

    if not new_jobs:
        logger.info("  Nothing to insert.")
        return {"inserted": 0, "skipped": skipped}

    inserted = 0
    for i, job in enumerate(new_jobs, 1):
        try:
            is_html = job.platform == "Greenhouse"
            children = _description_to_blocks(job.description, is_html=is_html)
            properties = _job_to_notion_properties(job)

            notion.pages.create(
                parent={"database_id": JD_ID},
                properties=properties,
                children=children,
            )
            inserted += 1

            if i % 10 == 0 or i == len(new_jobs):
                logger.info(f"    Inserted {i}/{len(new_jobs)} jobs...")

        except Exception as exc:
            logger.error(
                f"  Failed to insert '{job.title}' ({job.platform}/{job.id}): {exc}"
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
        slugs:   List of company slugs to scan. Defaults to SLUGS.
        timeout: Request timeout in seconds per slug.

    Returns:
        dict with keys:
            total    — number of matching jobs found
            matches  — list of JobPosting instances
            errors   — list of slugs that failed
    """
    load_config()
    slugs = slugs or SLUGS
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
        slugs:   List of company slugs to scan. Defaults to SLUGS.
        timeout: Request timeout in seconds per slug.

    Returns:
        dict with keys:
            total    — number of matching jobs found
            matches  — list of JobPosting instances
            errors   — list of slugs that failed
    """
    load_config()
    slugs = slugs or SLUGS
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
        slugs:   List of company slugs to scan. Defaults to SLUGS.
        timeout: Request timeout in seconds per slug (default 15 for Lever).

    Returns:
        dict with keys:
            total    — number of matching jobs found
            matches  — list of JobPosting instances
            errors   — list of slugs that failed
    """
    load_config()
    slugs = slugs or SLUGS
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

    storage = store_results(results["matches"])
    print(f"\n{'='*50}")
    print(
        f"Notion storage: {storage['inserted']} inserted, "
        f"{storage['skipped']} duplicates skipped"
    )
