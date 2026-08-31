"""
verify_pdf.py
=============
ATS PDF text-layer coverage checker.

After a tailored PDF is generated, this module extracts the raw text layer
(as ATS scanners do) and verifies that the bold_keywords actually appear.

Usage:
    from verify_pdf import verify_pdf_coverage
    report = verify_pdf_coverage(pdf_path, bold_keywords, jd_text)
    # report["verdict"] → "Pass" | "Borderline" | "Fail"
"""

from __future__ import annotations
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_pdf_text(pdf_path: Path) -> str:
    """
    Extract the full text layer from a PDF using pypdf.
    Returns empty string if extraction fails.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
        return "\n".join(parts)
    except Exception as e:
        logger.warning(f"  PDF text extraction failed: {e}")
        return ""


def _kw_in_text(kw: str, text: str) -> bool:
    """Case-insensitive whole-word match, also checks no-space variant."""
    text_lower = text.lower()
    kw_lower   = kw.lower()
    if re.search(r'(?i)\b' + re.escape(kw) + r'\b', text):
        return True
    # Try no-space: "Spring Boot" → "springboot"
    nospace = kw_lower.replace(" ", "")
    if nospace != kw_lower and nospace in text_lower:
        return True
    return False


def verify_pdf_coverage(
    pdf_path: Path,
    bold_keywords: list[str],
    jd_text: str = "",
    min_coverage: float = 0.60,
) -> dict:
    """
    Verify that bold_keywords appear in the PDF text layer.

    Returns:
        {
          "pdf_text_length": int,       # chars extracted
          "keywords_checked": list,     # all keywords tested
          "keywords_present": list,     # found in PDF text
          "keywords_missing": list,     # NOT found (ATS will miss them)
          "coverage_pct": float,        # 0-100
          "verdict": "Pass"|"Borderline"|"Fail",
          "verdict_color": "green"|"orange"|"red",
          "issues": list[str],          # human-readable warnings
          "recommendations": list[str], # actionable fixes
        }
    """
    pdf_text = extract_pdf_text(pdf_path) if pdf_path and Path(pdf_path).exists() else ""

    issues      : list[str] = []
    recs        : list[str] = []

    # ── 1. Check whether text layer exists at all ──────────────────────────
    if len(pdf_text.strip()) < 100:
        issues.append("PDF text layer is nearly empty — ATS cannot parse this PDF. "
                       "Possible causes: fonts embedded as images, or PDF is image-only.")
        recs.append("🔴 Re-generate PDF using headless Chrome (CDP); avoid image-based fonts.")
        return {
            "pdf_text_length": len(pdf_text),
            "keywords_checked": bold_keywords,
            "keywords_present": [],
            "keywords_missing": bold_keywords,
            "coverage_pct": 0.0,
            "verdict": "Fail",
            "verdict_color": "red",
            "issues": issues,
            "recommendations": recs,
        }

    # ── 2. Keyword coverage ─────────────────────────────────────────────────
    present = [k for k in bold_keywords if _kw_in_text(k, pdf_text)]
    missing = [k for k in bold_keywords if k not in present]
    total   = len(bold_keywords)
    coverage = round(len(present) / max(total, 1) * 100, 1)

    if missing:
        issues.append(
            f"{len(missing)} keyword(s) appear in HTML but NOT in extracted PDF text: "
            + ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        )
        recs.append(
            f"🟡 Keywords not reaching ATS text layer: {', '.join(missing[:5])}"
            + (" + more" if len(missing) > 5 else "")
            + " — Check font embedding & CSS visibility."
        )

    # ── 3. Section presence check ───────────────────────────────────────────
    tl = pdf_text.lower()
    section_checks = {
        "contact (email)":  bool(re.search(r'@[a-z]', tl)),
        "experience":       bool(re.search(r'experience|employment|work history', tl)),
        "skills":           bool(re.search(r'skills|technologies|tech stack', tl)),
        "education":        bool(re.search(r'education|university|b\.tech|bachelor|master', tl)),
    }
    for section, found in section_checks.items():
        if not found:
            issues.append(f"Section '{section}' not detected in PDF text layer.")
            recs.append(f"🔴 Section '{section}' missing from ATS-readable text — verify HTML structure.")

    # ── 4. En-dash / control-char contamination ─────────────────────────────
    if "\u2013" in pdf_text or "\u2014" in pdf_text:
        issues.append("PDF text contains en-dashes (–) or em-dashes (—). "
                       "Some ATS parsers split tokens at these chars (e.g. '2024–2025' → '2024' + '2025').")
        recs.append("🟡 Replace — and – with plain hyphens (-) in all date ranges and bullet points.")

    # ── 5. Overall verdict ──────────────────────────────────────────────────
    critical = sum(1 for i in issues if i.startswith(("PDF text layer", "Section 'contact")))
    if critical > 0 or coverage < 40:
        verdict, color = "Fail", "red"
    elif coverage < min_coverage * 100 or len(missing) > 3:
        verdict, color = "Borderline", "orange"
    else:
        verdict, color = "Pass", "green"

    if not recs:
        recs.append(f"✅ PDF text layer looks clean — {coverage}% keyword coverage.")

    logger.info(
        f"  PDF verify: {len(present)}/{total} keywords present "
        f"({coverage}%) → {verdict}"
    )

    return {
        "pdf_text_length":  len(pdf_text),
        "keywords_checked": bold_keywords,
        "keywords_present": present,
        "keywords_missing": missing,
        "coverage_pct":     coverage,
        "verdict":          verdict,
        "verdict_color":    color,
        "issues":           issues,
        "recommendations":  recs,
    }
