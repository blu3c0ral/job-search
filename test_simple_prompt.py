#!/usr/bin/env python3
"""
Baseline test: run resume tailoring with a minimal prompt (monkey-patched).
Does NOT modify resume_tailoring.py — patches at runtime only.
"""
import logging
import os
import time
import json

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

import anthropic
from supabase import create_client

import resume_tailoring

# ── Monkey-patch the prompt ───────────────────────────────────────────────────

resume_tailoring._SYSTEM_PROMPT = """\
You are an expert resume writer optimizing for ATS systems and hiring managers."""

_original_build_prompt = resume_tailoring._build_user_prompt


def _simple_prompt(editable_paras, jd, profile):
    from typing import Optional
    para_block = "\n".join(
        f'[{i}] {p["text"]}' for i, p in enumerate(editable_paras)
    )

    profile_section = ""
    if profile:
        profile_section = f"\n## Candidate Deep Profile\n{profile}\n"

    return f"""Tailor the resume paragraphs below to better match the job description. \
Optimize for ATS keyword matching and hiring manager relevance.

## Job Description
{jd}
{profile_section}
## Resume Paragraphs
Each line is prefixed with [N].

## Rules
- Do NOT invent any information that doesn't exist in the original resume. \
No fake skills, metrics, tools, or achievements. Rephrasing existing content is fine.
- Reorder clauses within bullets to lead with the most relevant facet.
- For keyword/skill lists (▪ or comma separated): reorder items for relevance. \
Do not add new items.
- If a line has a "Label: description" pattern, keep the label and colon intact.
- Keep the same length. Same tone. No "I".
- Return UNCHANGED paragraphs that already fit or can't be improved without inventing.

## Output Format

Return a JSON object with three keys:
- "paragraphs": object mapping index (string) to text (original or rewritten)
- "rationale": object mapping index (string) to one-sentence reason for each \
paragraph that was CHANGED (omit unchanged ones)
- "gaps": array of JD requirements not covered by the resume

No preamble, no markdown fences.

## Paragraphs:
{para_block}"""


resume_tailoring._build_user_prompt = _simple_prompt

# ── Setup ─────────────────────────────────────────────────────────────────────

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)
client = anthropic.Anthropic()

from resume_tailoring import tailor_resume_bytes, _load_config_from_env

TAILOR_STORAGE_BUCKET = "tailored-resumes"
RESUME_STORAGE_PATH = os.environ.get("RESUME_STORAGE_PATH", "")

# ── Download base resume ──────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("SIMPLE PROMPT BASELINE TEST")
print("=" * 60)

resume_bytes = supabase.storage.from_(TAILOR_STORAGE_BUCKET).download(RESUME_STORAGE_PATH)

import tempfile
resume_tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
resume_tmp.write(resume_bytes)
resume_tmp.close()
resume_path = resume_tmp.name

config = _load_config_from_env()

# ── Fetch two specific JDs (Scale AI + OpenAI) ───────────────────────────────

rows = (
    supabase.table("job_search_main")
    .select("id, source_platform, role_title, company, job_description, match")
    .in_("company", ["Scale AI", "Scale", "OpenAI"])
    .not_.is_("job_description", "null")
    .limit(2)
    .execute()
    .data
)

if not rows:
    print("  Scale/OpenAI jobs not found, falling back to any Good/Excelent match...")
    rows = (
        supabase.table("job_search_main")
        .select("id, source_platform, role_title, company, job_description, match")
        .in_("match", ["Excelent Match", "Good Match"])
        .not_.is_("job_description", "null")
        .limit(2)
        .execute()
        .data
    )

# ── Run tailoring on each ─────────────────────────────────────────────────────

for job in rows:
    print(f"\n{'=' * 60}")
    print(f"JOB: {job['company']} — {job['role_title']}")
    print(f"Match: {job.get('match', 'N/A')}")
    print("=" * 60)

    t0 = time.monotonic()
    docx_bytes, changes, gaps = tailor_resume_bytes(
        resume_path,
        job["job_description"],
        config=config,
        anthropic_client=client,
    )
    elapsed = time.monotonic() - t0

    print(f"\n  Completed in {elapsed:.1f}s")
    print(f"  Changes: {len(changes)}, Gaps: {len(gaps)}")

    for i, change in enumerate(changes, 1):
        print(f"\n  [{i}/{len(changes)}]")
        print(f"  BEFORE: {change['original'][:120]}")
        print(f"  AFTER:  {change['tailored'][:120]}")
        if "why" in change:
            print(f"  WHY:    {change['why']}")

    if gaps:
        print(f"\n  GAPS:")
        for g in gaps:
            print(f"    - {g}")

# ── Cleanup ───────────────────────────────────────────────────────────────────

import os as _os
_os.unlink(resume_path)

print("\n" + "=" * 60)
print("DONE — simple prompt baseline complete")
print("=" * 60)
