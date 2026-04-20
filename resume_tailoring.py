"""
resume_tailor.py
----------------
Tailors a .docx resume to a job description using the Anthropic API.

Approach:
  1. Unpack the .docx (ZIP) and parse document.xml
  2. Identify editable paragraphs (skip headers, titles, dates, etc.)
  3. Send only the editable text to Claude with the JD + optional profile
  4. Claude returns a JSON map of { index -> rewritten_text }
  5. Surgically swap <w:t> content in the XML, preserving all formatting
     including bold/non-bold boundaries within the same paragraph
  6. Repack into a new .docx

Configuration:
  Set RESUME_TAILOR_CONFIG env var as a JSON string (or RESUME_TAILOR_CONFIG_PATH
  pointing to a .json file) with keys that define which paragraphs to freeze.

  Example:
    {
      "name": "JANE DOE",
      "contact_prefix": "New York",
      "section_headers": ["WORK EXPERIENCE", "EDUCATION", "PERSONAL PROJECTS",
                          "TECHNICAL PROFILE", "AREAS OF IMPACT"],
      "title_prefixes": ["SOFTWARE", "QUANTITATIVE", "SENIOR"],
      "tech_labels": ["Programming Languages", "Cloud Infrastructure",
                      "Environment", "AI/ML Frameworks"],
      "education_prefixes": ["Associate", "Bachelor", "Master", "Ph.D"],
      "frozen_terms": ["US citizen"],
      "resume_name": "Jane Resume",
      "min_edit_length": 25,
      "model": "claude-sonnet-4-20250514"
    }

Entry points:
    tailor_resume(resume_path, jd, output_path, ...) -> str       # writes .docx file
    tailor_resume_bytes(resume_path, jd, ...) -> bytes             # returns .docx bytes
"""

import logging
import os
import re
import json
import zipfile
import tempfile
import anthropic

from dotenv import load_dotenv
from xml.etree import ElementTree as etree
from pathlib import Path
from typing import Optional

load_dotenv()

logger = logging.getLogger("resume_tailor")


def _configure_logging() -> None:
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

# ── Namespace ─────────────────────────────────────────────────────────────────

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = lambda tag: f"{{{W_NS}}}{tag}"  # noqa: E731
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MIN_EDIT_LENGTH = 25
DEFAULT_TEMPERATURE = 0.3

# Date-range pattern: "2018 – 2022", "2/2023 – 11/2024", "7/2025 – present"
DATE_RANGE_RE = re.compile(
    r"(\d{1,2}/)?\d{4}\s*[-–—]\s*((\d{1,2}/)?\d{4}|present)", re.IGNORECASE
)


# ── Config loading ────────────────────────────────────────────────────────────

def _load_config_from_env() -> dict:
    """Load config from RESUME_TAILOR_CONFIG (JSON string) or _CONFIG_PATH."""
    raw = os.environ.get("RESUME_TAILOR_CONFIG", "")
    if not raw:
        path = os.environ.get("RESUME_TAILOR_CONFIG_PATH", "")
        if path and Path(path).exists():
            raw = Path(path).read_text(encoding="utf-8")
    if not raw:
        return {}
    return json.loads(raw, strict=False)


# ── Frozen paragraph detection ────────────────────────────────────────────────

def _build_frozen_checker(config: dict):
    """
    Return a callable (text: str) -> bool that decides if a paragraph should
    be left untouched. Uses config keys to identify structural elements.
    """
    name = config.get("name", "")
    contact_prefix = config.get("contact_prefix", "")
    headers = {h.upper() for h in config.get("section_headers", [])}
    title_prefixes = config.get("title_prefixes", [])
    tech_labels = config.get("tech_labels", [])
    edu_prefixes = config.get("education_prefixes", [])
    frozen_terms = config.get("frozen_terms", [])
    min_len = config.get("min_edit_length", DEFAULT_MIN_EDIT_LENGTH)

    def is_frozen(text: str) -> bool:
        t = text.strip()

        # Empty or very short lines are structural
        if not t or len(t) < min_len:
            return True

        # Exact name match
        if name and t.upper() == name.upper():
            return True

        # Contact line
        if contact_prefix and t.startswith(contact_prefix):
            return True

        # Section headers (case-insensitive)
        if t.upper() in headers:
            return True

        # Job title prefixes (e.g., "SOFTWARE ENGINEER", "QUANTITATIVE ANALYST")
        if any(t.upper().startswith(p.upper()) for p in title_prefixes):
            return True

        # Technical profile label rows ("Programming Languages: ...")
        if any(t.startswith(label) for label in tech_labels):
            return True

        # Education lines
        if any(t.startswith(prefix) for prefix in edu_prefixes):
            return True

        # Arbitrary frozen terms (substring match)
        if any(term in t for term in frozen_terms):
            return True

        # Lines with a date range that are short → company/date headers
        if DATE_RANGE_RE.search(t) and len(t) < 100:
            return True

        return False

    return is_frozen


# ── XML text helpers ──────────────────────────────────────────────────────────

def _para_full_text(para_el) -> str:
    """All text in a paragraph, including text inside hyperlinks.
    Used for frozen detection where we need the complete picture."""
    parts = []
    for t in para_el.iter(W("t")):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def _para_editable_text(para_el) -> str:
    """Text from direct <w:r> children only — excludes hyperlinks.
    This is what Claude sees and can rewrite."""
    parts = []
    for child in para_el:
        if child.tag == W("r"):
            t = child.find(W("t"))
            if t is not None and t.text:
                parts.append(t.text)
    return "".join(parts)


# ── Multi-format run handling ─────────────────────────────────────────────────

def _is_bold(run_el) -> bool:
    """Check if a run has bold formatting."""
    rpr = run_el.find(W("rPr"))
    if rpr is None:
        return False
    return rpr.find(W("b")) is not None


def _get_format_segments(para_el) -> list[dict]:
    """
    Group consecutive direct-child <w:r> runs by bold vs. non-bold.

    Returns:
        [{"text": str, "runs": [lxml elements], "bold": bool}, ...]

    Skips runs with no <w:t> text (tab-only, break-only).
    Only examines direct children — runs inside <w:hyperlink> are ignored.
    """
    segments: list[dict] = []
    current: dict | None = None

    for child in para_el:
        if child.tag != W("r"):
            continue
        t_el = child.find(W("t"))
        if t_el is None or not t_el.text:
            continue

        bold = _is_bold(child)

        if current is None or current["bold"] != bold:
            if current is not None:
                segments.append(current)
            current = {"text": t_el.text, "runs": [child], "bold": bold}
        else:
            current["text"] += t_el.text
            current["runs"].append(child)

    if current is not None:
        segments.append(current)

    return segments


def _apply_text_to_segment(para_el, segment: dict, text: str) -> None:
    """Set text on a segment's first run and remove all extra runs."""
    first_run = segment["runs"][0]
    t_el = first_run.find(W("t"))
    if t_el is None:
        t_el = etree.SubElement(first_run, W("t"))

    t_el.text = text
    if text != text.strip():
        t_el.set(XML_SPACE, "preserve")
    else:
        t_el.attrib.pop(XML_SPACE, None)

    for r in segment["runs"][1:]:
        para_el.remove(r)


def _set_para_text(para_el, new_text: str) -> None:
    """
    Replace paragraph text while preserving formatting structure.

    - Single-format paragraph: all text goes into the first run.
    - Multi-format paragraph (bold label + non-bold body): detects the ":"
      delimiter and splits new text to preserve the formatting boundary.
    - Hyperlinks and other non-run elements are untouched.
    """
    segments = _get_format_segments(para_el)

    if not segments:
        return

    # ── Single format: simple replace ────────────────────────────────────
    if len(segments) == 1:
        _apply_text_to_segment(para_el, segments[0], new_text)
        return

    # ── Multi-format: split at ":" boundary ──────────────────────────────
    first_text = segments[0]["text"].rstrip()
    has_colon_boundary = first_text.endswith(":")

    if has_colon_boundary and ":" in new_text:
        colon_idx = new_text.index(":") + 1
        label_part = new_text[:colon_idx]
        body_part = new_text[colon_idx:]
    else:
        # Fallback: keep the original label, replace body with full new text
        label_part = segments[0]["text"]
        body_part = new_text

    _apply_text_to_segment(para_el, segments[0], label_part)

    if len(segments) > 1:
        _apply_text_to_segment(para_el, segments[1], body_part)

    # Remove any extra segments beyond the first two
    for seg in segments[2:]:
        for r in seg["runs"]:
            para_el.remove(r)


# ── Docx I/O ─────────────────────────────────────────────────────────────────

def _unpack_docx(docx_path: str, dest_dir: str) -> None:
    with zipfile.ZipFile(docx_path, "r") as z:
        z.extractall(dest_dir)


def _pack_docx(src_dir: str, output_path: str) -> None:
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                arc_name = os.path.relpath(abs_path, src_dir)
                z.write(abs_path, arc_name)


def _register_namespaces(xml_path: str) -> None:
    """Register all namespace prefixes from the XML so they survive round-trip."""
    with open(xml_path, "rb") as f:
        header = f.read(4096)
    for m in re.finditer(rb'xmlns:(\w+)="([^"]+)"', header):
        etree.register_namespace(m.group(1).decode(), m.group(2).decode())


def _load_document_xml(unpacked_dir: str):
    doc_path = os.path.join(unpacked_dir, "word", "document.xml")
    _register_namespaces(doc_path)
    tree = etree.parse(doc_path)
    return tree, doc_path


def _save_document_xml(tree, doc_path: str) -> None:
    tree.write(doc_path, xml_declaration=True, encoding="UTF-8")
    # Re-insert standalone="yes" to match original .docx convention
    with open(doc_path, "rb") as f:
        content = f.read()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
    )
    with open(doc_path, "wb") as f:
        f.write(content)


# ── Claude interaction ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert resume writer. You tailor resume bullet points to better match \
a target job description. You are strategic: surface the most relevant aspects of \
existing experience, rephrase to mirror the language of the JD, and reorder keywords \
for maximum impact — but you never fabricate."""


def _build_user_prompt(
    editable_paras: list[dict], jd: str, profile: Optional[str]
) -> str:
    para_block = "\n".join(
        f'[{i}] {p["text"]}' for i, p in enumerate(editable_paras)
    )

    profile_section = ""
    if profile:
        profile_section = f"\n## Candidate Deep Profile\n{profile}\n"

    return f"""Review the resume paragraphs below and tailor them to better match \
the job description.

## Job Description
{jd}
{profile_section}
## Resume Paragraphs
Each line is prefixed with [N].

Rules:
- Return the SAME number of paragraphs. One entry per index.
- For each paragraph, decide: does it have a stronger angle to surface for this JD? \
If yes, rephrase it — reorder clauses, mirror the JD's language, shift emphasis to \
the most relevant parts of the existing content. \
If the paragraph already aligns well and cannot be improved, return it UNCHANGED.
- Keep roughly the same length — do not expand or shrink significantly.
- CRITICAL: Do NOT invent any data, facts, numbers, achievements, technologies, \
or responsibilities not present in the SAME paragraph's original text. Every word \
you add or change must be grounded in what is already written in THAT paragraph. \
Do not move content between paragraphs. Rephrasing, reordering, and shifting \
emphasis is encouraged; fabrication or misrepresentation of what the work actually \
was is not. "Chat pipeline" must not become "data pipeline" — describe the same work, \
just highlight its most relevant facets.
- For keyword/skill lists (items separated by ▪ or commas): reorder to put the \
most JD-relevant items first. Do not add items that are not already in that list.
- Do NOT rename proper nouns: company names, product names, technologies, \
programming languages.
- If a line has a "Label: description" pattern (e.g., "ProjectName: Built..."), \
keep the label and colon intact. Only rewrite the description part.
- Preserve the tone: confident, specific, first-person implied (no "I").
- Output ONLY a JSON object mapping index (string) to text (original or rewritten). \
No preamble, no markdown fences, no explanation.

Example: {{"0": "rewritten text", "1": "unchanged original text", "2": "rewritten text"}}

## Paragraphs:
{para_block}"""


def _parse_json_response(raw: str) -> dict[str, str]:
    """Parse Claude's response, stripping markdown fences if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def _call_claude(
    prompt: str, model: str, anthropic_client=None,
) -> dict[str, str]:
    client = anthropic_client or anthropic.Anthropic()

    for attempt in range(2):
        message = client.messages.create(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=DEFAULT_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text
        try:
            return _parse_json_response(raw)
        except (json.JSONDecodeError, KeyError):
            if attempt == 0:
                logger.warning("JSON parse failed, retrying...")
                continue
            raise ValueError(f"Claude returned invalid JSON after 2 attempts: {raw[:200]}")


# ── Core pipeline ─────────────────────────────────────────────────────────────

def _extract_editable_paragraphs(tree, is_frozen) -> list[dict]:
    """
    Walk paragraphs in the document body. Return editable ones as:
        [{"el": lxml element, "text": str (direct-run text only)}, ...]
    """
    body = tree.getroot().find(f".//{W('body')}")
    editable = []
    for para in body.iter(W("p")):
        full_text = _para_full_text(para)
        if is_frozen(full_text):
            continue
        edit_text = _para_editable_text(para)
        if edit_text.strip():
            editable.append({"el": para, "text": edit_text.strip()})
    return editable


def _apply_rewrites(
    editable_paras: list[dict], rewrites: dict[str, str],
) -> list[dict]:
    """Apply rewrites and return a changelog of what changed."""
    changes = []
    for i, para in enumerate(editable_paras):
        key = str(i)
        if key in rewrites and rewrites[key].strip():
            new_text = rewrites[key].strip()
            if new_text != para["text"]:
                changes.append({"original": para["text"], "tailored": new_text})
                _set_para_text(para["el"], new_text)
    return changes


# ── Public API ────────────────────────────────────────────────────────────────

def _tailor_core(
    resume_path: str,
    jd: str,
    output_path: str,
    profile: Optional[str] = None,
    config: Optional[dict] = None,
    anthropic_client=None,
) -> tuple[str, list[dict]]:
    """
    Core tailoring pipeline. Writes the tailored .docx to output_path.
    Returns (absolute_output_path, changelog).
    """
    if config is None:
        config = _load_config_from_env()

    model = config.get("model", DEFAULT_MODEL)
    is_frozen = _build_frozen_checker(config)

    resume_path = str(Path(resume_path).resolve())
    output_path = str(Path(output_path).resolve())

    with tempfile.TemporaryDirectory() as tmpdir:
        _unpack_docx(resume_path, tmpdir)
        tree, doc_path = _load_document_xml(tmpdir)

        editable = _extract_editable_paragraphs(tree, is_frozen)
        if not editable:
            raise ValueError("No editable paragraphs found — check your config.")

        logger.info(f"{len(editable)} editable paragraphs found.")

        prompt = _build_user_prompt(editable, jd, profile)
        logger.info(f"Calling {model}...")
        rewrites = _call_claude(prompt, model, anthropic_client=anthropic_client)
        logger.info(f"Received {len(rewrites)} rewrites.")

        changes = _apply_rewrites(editable, rewrites)

        _save_document_xml(tree, doc_path)
        _pack_docx(tmpdir, output_path)

    return output_path, changes


def tailor_resume(
    resume_path: str,
    jd: str,
    output_path: str,
    profile: Optional[str] = None,
    config: Optional[dict] = None,
    anthropic_client=None,
) -> tuple[str, list[dict]]:
    """
    Tailor a .docx resume to a job description.

    Args:
        resume_path:       Path to the original resume .docx file.
        jd:                Job description text (plain string).
        output_path:       Where to write the tailored .docx.
        profile:           Optional deep-profile text for richer context.
        config:            Optional config dict. If None, loads from env var
                           RESUME_TAILOR_CONFIG or RESUME_TAILOR_CONFIG_PATH.
        anthropic_client:  Optional Anthropic client instance to reuse.

    Returns:
        (absolute_output_path, changelog) where changelog is a list of
        {"original": str, "tailored": str} dicts.
    """
    result, changes = _tailor_core(
        resume_path, jd, output_path,
        profile=profile, config=config, anthropic_client=anthropic_client,
    )
    logger.info(f"Done → {result} ({len(changes)} paragraphs changed)")
    return result, changes


def tailor_resume_bytes(
    resume_path: str,
    jd: str,
    profile: Optional[str] = None,
    config: Optional[dict] = None,
    anthropic_client=None,
) -> tuple[bytes, list[dict]]:
    """
    Tailor a .docx resume and return the result as bytes (for upload).

    Returns:
        (docx_bytes, changelog) where changelog is a list of
        {"original": str, "tailored": str} dicts.
    """
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        _, changes = _tailor_core(
            resume_path, jd, tmp_path,
            profile=profile, config=config, anthropic_client=anthropic_client,
        )
        return Path(tmp_path).read_bytes(), changes
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Tailor a .docx resume to a job description."
    )
    parser.add_argument("resume", help="Path to original resume .docx")
    parser.add_argument("jd", help="Path to job description .txt file")
    parser.add_argument("output", help="Path for tailored output .docx")
    parser.add_argument(
        "--profile", help="Path to deep-profile .txt file", default=None
    )
    parser.add_argument(
        "--config", help="Path to config .json file (overrides env)", default=None
    )
    args = parser.parse_args()

    jd_text = Path(args.jd).read_text(encoding="utf-8")
    profile_text = (
        Path(args.profile).read_text(encoding="utf-8") if args.profile else None
    )
    cfg = (
        json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.config
        else None
    )

    tailor_resume(args.resume, jd_text, args.output, profile=profile_text, config=cfg)