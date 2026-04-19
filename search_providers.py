"""
Search provider abstraction for job discovery.

Provides a provider-agnostic interface so the pipeline in web_job_search.py
does not depend on any specific search API.  Currently implements SerpAPI's
google_jobs engine.
"""

import logging
import re
import time
from abc import ABC, abstractmethod

import requests
from pydantic import BaseModel, Field

logger = logging.getLogger("web_job_search")

# ─── Normalized Result Model ────────────────────────────────────────────────


class JobSearchResult(BaseModel):
    """Provider-agnostic normalized job search result."""

    job_id: str
    title: str
    company: str
    location: str
    description: str
    compensation: str = "Not listed"
    posted_at: str | None = None       # "2 days ago", etc.
    schedule_type: str | None = None   # "Full-time", "Contract", etc.
    work_from_home: bool | None = None
    source: str | None = None          # via field — "LinkedIn", "Stripe Careers"
    apply_options: list[dict] = Field(default_factory=list)
    highlights: list[dict] = Field(default_factory=list)
    query: str = ""


# ─── Abstract Provider ──────────────────────────────────────────────────────


class SearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, location: str, **kwargs) -> list[JobSearchResult]:
        """Execute a single search query. Returns normalized results."""
        ...

    @abstractmethod
    def name(self) -> str: ...


# ─── SerpAPI google_jobs Provider ────────────────────────────────────────────

SERPAPI_URL = "https://serpapi.com/search"

# Regex to find salary-like strings in extensions: "$150K", "$180,000 - $220,000", etc.
_SALARY_RE = re.compile(r"\$[\d,.]+[KkMm]?(?:\s*[-–]\s*\$[\d,.]+[KkMm]?)?(?:\s*(?:a year|per year|annually))?")


class SerpApiProvider(SearchProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def name(self) -> str:
        return "SerpAPI"

    def search(
        self,
        query: str,
        location: str,
        chips: str = "date_posted:week",
        max_pages: int = 1,
    ) -> list[JobSearchResult]:
        """Execute google_jobs search. Returns normalized results.

        Args:
            chips: Freshness filter — "date_posted:today", "date_posted:3days",
                   "date_posted:week", "date_posted:month".
            max_pages: Max pages to fetch (10 results/page). Each page = 1 API call.
        """
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "chips": chips,
            "api_key": self.api_key,
        }

        all_results: list[JobSearchResult] = []

        for page in range(max_pages):
            try:
                resp = requests.get(SERPAPI_URL, params=params, timeout=15)

                if resp.status_code == 429:
                    logger.warning("  SerpAPI rate limited, waiting 30s...")
                    time.sleep(30)
                    resp = requests.get(SERPAPI_URL, params=params, timeout=15)

                if resp.status_code != 200:
                    logger.warning(
                        f"  SerpAPI search failed (HTTP {resp.status_code}) for: {query[:60]}"
                        f" — {resp.text[:200]}"
                    )
                    break

                data = resp.json()
                jobs = data.get("jobs_results", [])
                if not jobs:
                    break

                for job in jobs:
                    all_results.append(self._normalize(job, query))

                # Paginate only if we got a full page (10 results) and have a token
                next_token = (data.get("serpapi_pagination") or {}).get("next_page_token")
                if len(jobs) < 10 or not next_token or page + 1 >= max_pages:
                    break
                params["next_page_token"] = next_token

                time.sleep(1)  # courtesy delay between pages

            except Exception as exc:
                logger.error(f"  SerpAPI search error: {exc}")
                break

        return all_results

    def _normalize(self, job: dict, query: str) -> JobSearchResult:
        """Map one SerpAPI jobs_results item to a JobSearchResult."""
        detected = job.get("detected_extensions") or {}
        extensions = job.get("extensions") or []

        return JobSearchResult(
            job_id=job.get("job_id", ""),
            title=job.get("title", ""),
            company=job.get("company_name", ""),
            location=job.get("location", ""),
            description=job.get("description", ""),
            compensation=self._parse_compensation(extensions),
            posted_at=detected.get("posted_at"),
            schedule_type=detected.get("schedule_type"),
            work_from_home=detected.get("work_from_home"),
            source=job.get("via", ""),
            apply_options=job.get("apply_options") or [],
            highlights=job.get("job_highlights") or [],
            query=query,
        )

    @staticmethod
    def _parse_compensation(extensions: list[str]) -> str:
        """Extract salary from extensions list."""
        for ext in extensions:
            if _SALARY_RE.search(ext):
                return ext.strip()
        return "Not listed"
