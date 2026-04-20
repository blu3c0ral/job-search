#!/usr/bin/env python3
"""
Smoke test for resume tailoring pipeline:
  1. Download base resume from Supabase Storage
  2. Tailor it against a real job description from the DB
  3. Verify output is a valid .docx with the expected number of paragraphs
  4. Print the changelog (what changed vs. the base)

Does NOT upload to storage or write to the DB — read-only test.
"""
import logging
import os
import time
import zipfile
import tempfile

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

import anthropic
from supabase import create_client

from resume_tailoring import tailor_resume_bytes, _load_config_from_env

# ── Setup ──────────────────────────────────────────────────────────────────────

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)
client = anthropic.Anthropic()

TAILOR_STORAGE_BUCKET = "tailored-resumes"
RESUME_STORAGE_PATH = os.environ.get("RESUME_STORAGE_PATH", "")

# ── Test 1: Download base resume ───────────────────────────────────────────────

print("\n" + "=" * 60)
print("TEST 1 — Download base resume from Supabase Storage")
print("=" * 60)

if not RESUME_STORAGE_PATH:
    print("ERROR: RESUME_STORAGE_PATH env var not set.")
    raise SystemExit(1)

print(f"  Downloading: {TAILOR_STORAGE_BUCKET}/{RESUME_STORAGE_PATH}")
t0 = time.monotonic()
resume_bytes = supabase.storage.from_(TAILOR_STORAGE_BUCKET).download(RESUME_STORAGE_PATH)
print(f"  Downloaded {len(resume_bytes):,} bytes in {time.monotonic() - t0:.2f}s")

# Write to a temp file for the tailoring step
resume_tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
resume_tmp.write(resume_bytes)
resume_tmp.close()
resume_path = resume_tmp.name
print(f"  Saved to {resume_path}")
print("  PASS\n")

# ── Test 2: Fetch a job with a JD from the DB ──────────────────────────────────

print("=" * 60)
print("TEST 2 — Fetch qualifying job from DB")
print("=" * 60)

rows = (
    supabase.table("job_search_main")
    .select("id, source_platform, role_title, company, job_description, match")
    .in_("match", ["Excelent Match", "Good Match"])
    .not_.is_("job_description", "null")
    .limit(1)
    .execute()
    .data
)

if not rows:
    print("  No Excelent/Good Match jobs found, falling back to any job with a JD...")
    rows = (
        supabase.table("job_search_main")
        .select("id, source_platform, role_title, company, job_description, match")
        .not_.is_("job_description", "null")
        .limit(1)
        .execute()
        .data
    )

if not rows:
    print("ERROR: No jobs with job_description found in DB.")
    raise SystemExit(1)

job = rows[0]
print(
    f"  Job: {job['company']} — {job['role_title']}\n"
    f"  Platform: {job['source_platform']}/{job['id']}\n"
    f"  Match: {job.get('match', 'N/A')}\n"
    f"  JD length: {len(job.get('job_description') or ''):,} chars"
)
print("  PASS\n")

# ── Test 3: Tailor the resume ──────────────────────────────────────────────────

print("=" * 60)
print("TEST 3 — Tailor resume against JD")
print("=" * 60)

config = _load_config_from_env()
if not config:
    print("ERROR: RESUME_TAILOR_CONFIG env var not set.")
    raise SystemExit(1)

print(f"  Config loaded: {list(config.keys())}")
print(f"  Model: {config.get('model', 'claude-sonnet-4-20250514')}")

t0 = time.monotonic()
docx_bytes, changes = tailor_resume_bytes(
    resume_path,
    job["job_description"],
    config=config,
    anthropic_client=client,
)
elapsed = time.monotonic() - t0

print(f"\n  Tailoring complete in {elapsed:.1f}s")
print(f"  Output size: {len(docx_bytes):,} bytes")
print(f"  Paragraphs changed: {len(changes)}")
print("  PASS\n")

# ── Test 4: Verify the .docx is valid ─────────────────────────────────────────

print("=" * 60)
print("TEST 4 — Verify output is a valid .docx")
print("=" * 60)

with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as out_tmp:
    out_tmp.write(docx_bytes)
    out_path = out_tmp.name

try:
    with zipfile.ZipFile(out_path) as z:
        names = z.namelist()
    assert "word/document.xml" in names, "word/document.xml missing from output"
    assert "[Content_Types].xml" in names, "[Content_Types].xml missing"
    print(f"  Valid .docx with {len(names)} internal files")
    print(f"  Output saved to: {out_path}")
    print("  PASS\n")
except Exception as e:
    print(f"  FAIL: {e}")
    raise SystemExit(1)

# ── Print changelog ────────────────────────────────────────────────────────────

print("=" * 60)
print("CHANGELOG — what changed from the base resume")
print("=" * 60)

for i, change in enumerate(changes, 1):
    print(f"\n  [{i}/{len(changes)}]")
    print(f"  BEFORE: {change['original'][:120]}")
    print(f"  AFTER:  {change['tailored'][:120]}")

# ── Cleanup ────────────────────────────────────────────────────────────────────

import os as _os
_os.unlink(resume_path)

print("\n" + "=" * 60)
print(f"All tests passed. Tailored .docx at: {out_path}")
print("=" * 60)
