#!/usr/bin/env python3
"""
Build a PDF from the question_bank table (same SQLite DB as quiz_scraper.py).
Correct answer(s) are green; incorrect options are red.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from typing import Any

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

# Accessible green / red on white
GREEN_HEX = "#0d6630"
RED_HEX = "#b00020"

# Fit inside SimpleDocTemplate body frame (tall images must not exceed frame height)
IMAGE_MAX_WIDTH = 5.5 * inch
IMAGE_MAX_HEIGHT = 6.25 * inch


def image_draw_size(path: str, max_w: float, max_h: float) -> tuple[float, float]:
    """Return (width, height) in points, preserving aspect ratio, bounded by max_w and max_h."""
    ir = ImageReader(path)
    iw, ih = ir.getSize()
    if iw <= 0 or ih <= 0:
        return max_w * 0.75, max_h * 0.25
    aspect = iw / float(ih)
    w = max_w
    h = w / aspect
    if h > max_h:
        h = max_h
        w = h * aspect
    return w, h


def xml_escape(text: str) -> str:
    return escape(text or "", {'"': "&quot;", "'": "&apos;"})


def parse_options(options_json: str) -> list[str]:
    try:
        raw = json.loads(options_json)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def correct_answer_set(correct_text: str, correct_index: int, options: list[str]) -> set[str]:
    """Normalized option strings that count as correct (supports multi-correct 'A | B')."""
    parts = [p.strip() for p in (correct_text or "").split("|") if p.strip()]
    if parts:
        return set(parts)
    if 0 <= correct_index < len(options):
        return {options[correct_index].strip()}
    return set()


def load_questions(conn: sqlite3.Connection) -> list[tuple[str, str, int, str, str | None]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(question_bank)").fetchall()}
    if cols and "image_relpath" in cols:
        cur = conn.execute(
            """
            SELECT question_text, options_json, correct_index, correct_text, image_relpath
            FROM question_bank
            ORDER BY id
            """
        )
        return [(a, b, c, d, e) for (a, b, c, d, e) in cur.fetchall()]
    cur = conn.execute(
        """
        SELECT question_text, options_json, correct_index, correct_text
        FROM question_bank
        ORDER BY id
        """
    )
    return [(a, b, c, d, None) for (a, b, c, d) in cur.fetchall()]


def build_flowables(
    rows: list[tuple[str, str, int, str, str | None]],
    title: str,
    *,
    media_base: Path,
) -> list[Any]:
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        name="Body",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        spaceAfter=4,
    )
    qstyle = ParagraphStyle(
        name="QuestionBlock",
        parent=styles["Heading3"],
        fontSize=11,
        leading=15,
        spaceAfter=8,
        spaceBefore=12,
    )
    out: list[Any] = []

    out.append(Paragraph(xml_escape(title), styles["Title"]))
    out.append(
        Paragraph(
            xml_escape(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — {len(rows)} question(s)"),
            styles["Normal"],
        )
    )
    out.append(Spacer(1, 0.25 * inch))

    for i, (qtext, options_json, correct_index, correct_text, image_relpath) in enumerate(rows, start=1):
        options = parse_options(options_json)
        correct_set = correct_answer_set(correct_text, correct_index, options)

        out.append(Paragraph(f"<b>{i}.</b> {xml_escape(qtext)}", qstyle))

        if image_relpath:
            img_path = (media_base / image_relpath).resolve()
            if img_path.is_file():
                try:
                    w, h = image_draw_size(str(img_path), IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT)
                    out.append(RLImage(str(img_path), width=w, height=h))
                    out.append(Spacer(1, 0.08 * inch))
                except OSError:
                    pass

        if not options:
            out.append(Paragraph("<i>(no options stored)</i>", body))
            out.append(Spacer(1, 0.15 * inch))
            continue

        for opt in options:
            o = opt.strip()
            if correct_set:
                is_ok = o in correct_set
            elif 0 <= correct_index < len(options):
                is_ok = o == options[correct_index].strip()
            else:
                is_ok = False
            hex_c = GREEN_HEX if is_ok else RED_HEX
            line = f'<font color="{hex_c}">{xml_escape(opt)}</font>'
            out.append(Paragraph(line, body))

        out.append(Spacer(1, 0.12 * inch))

    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Export question_bank to a colored PDF.")
    p.add_argument("--db", default="quiz_bank.sqlite", help="SQLite database path")
    p.add_argument("--out", default="quiz_bank.pdf", help="Output PDF path")
    p.add_argument("--title", default="Practice quiz bank", help="Document title")
    p.add_argument(
        "--media-base",
        default=None,
        metavar="DIR",
        help="Base directory for image_relpath values (default: directory containing --db)",
    )
    args = p.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    media_base = Path(args.media_base).expanduser().resolve() if args.media_base else db_path.parent

    conn = sqlite3.connect(str(db_path))
    try:
        rows = load_questions(conn)
    finally:
        conn.close()

    if not rows:
        raise SystemExit("No rows in question_bank; run the scraper first or point --db to your database.")

    doc = SimpleDocTemplate(
        args.out,
        pagesize=LETTER,
        rightMargin=inch * 0.75,
        leftMargin=inch * 0.75,
        topMargin=inch * 0.75,
        bottomMargin=inch * 0.75,
        title=args.title,
    )
    story = build_flowables(rows, args.title, media_base=media_base)
    doc.build(story)
    print(f"Wrote {args.out} ({len(rows)} questions)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
