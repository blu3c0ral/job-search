"""
tailor_single_job.py
--------------------
Tailor a resume for a single existing job in the Supabase table.
Fetches the row by (source_platform, id), runs resume tailoring,
uploads the result, and updates the row.

Usage:
    python tailor_single_job.py --platform Ashby --id abc123
"""

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from supabase import create_client

from resume_tailoring import tailor_resume_bytes

load_dotenv()

# ─── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("tailor_single_job")
handler = logging.StreamHandler()
handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ─── Constants ───────────────────────────────────────────────────────────────

TAILOR_STORAGE_BUCKET = "tailored-resumes"
TAILOR_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def main():
    parser = argparse.ArgumentParser(description="Tailor resume for a single job row.")
    parser.add_argument("--platform", required=True, help="source_platform value (e.g. Ashby, Greenhouse, Lever)")
    parser.add_argument("--id", required=True, help="Job ID (primary key together with platform)")
    args = parser.parse_args()

    platform = args.platform
    job_id = args.id

    # ─── Setup ────────────────────────────────────────────────────────────
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    client = anthropic.Anthropic()

    # ─── Fetch the job row ────────────────────────────────────────────────
    logger.info(f"Fetching job: platform={platform}, id={job_id}")
    result = (
        supabase.table("job_search_main")
        .select("*")
        .eq("source_platform", platform)
        .eq("id", job_id)
        .execute()
    )

    if not result.data:
        logger.error(f"No job found for platform={platform}, id={job_id}")
        raise SystemExit(1)

    row = result.data[0]
    company = row.get("company", "Unknown")
    title = row.get("role_title", "Unknown")
    jd = row.get("job_description", "")

    if not jd:
        logger.error(f"Job has no job_description — cannot tailor.")
        raise SystemExit(1)

    logger.info(f"Found: {company} — {title}")

    # ─── Load config ──────────────────────────────────────────────────────
    config_raw = os.environ.get("RESUME_TAILOR_CONFIG", "")
    if not config_raw:
        logger.error("RESUME_TAILOR_CONFIG not set — cannot tailor.")
        raise SystemExit(1)

    tailor_config = json.loads(config_raw)

    # ─── Download base resume ─────────────────────────────────────────────
    storage_path = os.environ.get("RESUME_STORAGE_PATH", "")
    if not storage_path:
        logger.error("RESUME_STORAGE_PATH not set — cannot tailor.")
        raise SystemExit(1)

    logger.info(f"Downloading base resume from storage: {storage_path}")
    import tempfile
    data = supabase.storage.from_(TAILOR_STORAGE_BUCKET).download(storage_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.write(data)
    tmp.close()
    resume_path = Path(tmp.name)

    # ─── Load profile ─────────────────────────────────────────────────────
    profile = os.environ.get("PROFILE_YAML")

    # ─── Tailor ───────────────────────────────────────────────────────────
    logger.info("Running resume tailoring...")
    t0 = time.monotonic()

    docx_bytes, changes, gaps = tailor_resume_bytes(
        str(resume_path),
        jd,
        profile=profile,
        config=tailor_config,
        anthropic_client=client,
    )

    resume_path.unlink(missing_ok=True)
    elapsed = time.monotonic() - t0
    logger.info(f"Tailoring complete: {len(changes)} changes, {len(gaps)} gaps ({elapsed:.1f}s)")

    # ─── Upload tailored resume ───────────────────────────────────────────
    resume_name = tailor_config.get("resume_name", "Resume")
    clean_company = re.sub(r'[^\w\s-]', '', company).strip()
    filename = f"{resume_name} - {clean_company}.docx"
    upload_path = f"{platform}/{filename}"

    logger.info(f"Uploading tailored resume to: {upload_path}")

    # Upsert: remove existing file first (ignore errors if it doesn't exist)
    try:
        supabase.storage.from_(TAILOR_STORAGE_BUCKET).remove([upload_path])
    except Exception:
        pass

    supabase.storage.from_(TAILOR_STORAGE_BUCKET).upload(
        upload_path,
        docx_bytes,
        {"content-type": TAILOR_CONTENT_TYPE},
    )

    # ─── Update the row ───────────────────────────────────────────────────
    logger.info("Updating job row with tailored resume path...")
    supabase.table("job_search_main").update({
        "tailored_resume": upload_path,
        "tailoring_changes": {"changes": changes, "gaps": gaps},
    }).eq("source_platform", platform).eq("id", job_id).execute()

    logger.info(f"Done! {company} — {title} -> {upload_path}")


if __name__ == "__main__":
    main()
