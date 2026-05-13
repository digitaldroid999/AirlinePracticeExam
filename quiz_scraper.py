#!/usr/bin/env python3
"""
Automate the AA Systems Practice Quiz (ASP.NET WebForms): PracMCL/MCP (radio
radDist), PracMAL (checkbox chkDist*), etc.

Intended for personal study only. You must comply with your employer/training
provider terms of use. If the site requires authentication, pass cookies from
your logged-in browser via --cookies (Netscape format).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://training.aapilots.com/private/ftpdr/Public/PracticeExams/Systems/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

FLEETS = {
    "A32F": "7",
    "B737": "1",
    "B777": "3",
    "B787": "8",
}


@dataclass
class QuizContext:
    session: requests.Session
    quiz_url: str
    last_response_url: str = ""


def load_mozilla_cookies(session: requests.Session, path: str) -> None:
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    for c in jar:
        session.cookies.set_cookie(c)


def asp_hidden_fields(form: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name or inp.get("type") != "hidden":
            continue
        out[name] = inp.get("value") or ""
    return out


def merge_visible_form_inputs(form: BeautifulSoup, data: dict[str, str]) -> None:
    """Add text/checkbox/select/textarea values ASP.NET may require on postback."""

    def add_if_absent(name: str, value: str) -> None:
        if name and name not in data:
            data[name] = value

    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = _input_type(inp)
        if itype in ("hidden", "submit", "button", "image", "radio"):
            continue
        if itype == "checkbox":
            if inp.has_attr("checked"):
                add_if_absent(name, inp.get("value") or "on")
            continue
        add_if_absent(name, inp.get("value") or "")

    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            add_if_absent(name, ta.get_text())

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name or name in data:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        if opt is not None:
            data[name] = opt.get("value") or opt.get_text() or ""


def resolve_submit_button_field(form: BeautifulSoup, button_display_value: str) -> tuple[str, str]:
    """Map 'Submit Answer' / 'Next Question' to the real name= attribute (may be ctl00$...$btnJudge)."""
    vwant = (button_display_value or "").strip()
    for inp in form.find_all("input"):
        if _input_type(inp) != "submit":
            continue
        v = (inp.get("value") or "").strip()
        if v == vwant:
            name = inp.get("name")
            if name:
                return name, v
    return "btnJudge", vwant


def resolve_radio_group_name_for_value(form: BeautifulSoup, selected_value: str) -> str:
    """Real radio group name for the selected option value (e.g. radDist1 under ctl00$...$radDist)."""
    for inp in form.find_all("input"):
        if _input_type(inp) != "radio":
            continue
        if inp.get("value") == selected_value and inp.get("name"):
            return str(inp.get("name"))
    names: set[str] = set()
    for inp in form.find_all("input"):
        if _input_type(inp) == "radio" and inp.get("name"):
            names.add(str(inp.get("name")))
    if len(names) == 1:
        return next(iter(names))
    if "radDist" in names:
        return "radDist"
    return "radDist"


def find_toolbar_submit(soup: BeautifulSoup) -> Any:
    btn = soup.find(id="btnJudge")
    if btn is not None:
        return btn
    form = soup.find("form", id="form1")
    if form is not None:
        for inp in form.find_all("input"):
            if _input_type(inp) == "submit":
                return inp
    for sid in ("div_Toolbar", "td_Buttons"):
        divtb = soup.find(id=sid)
        if divtb is not None:
            for inp in divtb.find_all("input"):
                if _input_type(inp) == "submit":
                    return inp
    return None


def find_checked_radio_in_form(form: BeautifulSoup) -> Any:
    for inp in form.find_all("input"):
        if _input_type(inp) == "radio" and inp.has_attr("checked"):
            return inp
    return None


def compose_quiz_post_data(form: BeautifulSoup, extra: dict[str, str]) -> dict[str, str]:
    data = dict(asp_hidden_fields(form))
    merge_visible_form_inputs(form, data)
    merged = dict(data)
    merged.update(extra)
    if "btnJudge" in merged:
        val = merged.pop("btnJudge")
        sn, sv = resolve_submit_button_field(form, val)
        merged[sn] = sv
    return merged


def parse_title_numbers(title: str | None) -> tuple[int | None, int | None]:
    if not title:
        return None, None
    m = re.search(r"Question\s+(\d+)\s+of\s+(\d+)", title, re.I)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _input_type(inp: Any) -> str:
    """Normalize HTML input type (attribute casing varies; some pages omit type)."""
    if inp is None:
        return ""
    t = inp.get("type")
    return (t or "").strip().lower()


def collect_radio_inputs(soup: BeautifulSoup) -> list[Any]:
    """Return visible radio inputs; search form first, then whole document."""
    roots: list[Any] = []
    form = soup.find("form", id="form1")
    if form is not None:
        roots.append(form)
    div_main = soup.find(id="div_Main")
    if div_main is not None and div_main not in roots:
        roots.append(div_main)
    if soup not in roots:
        roots.append(soup)

    radios: list[Any] = []
    seen: set[int] = set()
    for root in roots:
        for inp in root.find_all("input"):
            tid = id(inp)
            if tid in seen:
                continue
            seen.add(tid)
            t = _input_type(inp)
            if t == "radio":
                radios.append(inp)
                continue
            # Rare: radio-like MC without explicit type=radio (still posts as a group)
            if t == "" and "chkdist" in " ".join(inp.get("class") or []).lower():
                radios.append(inp)
    return radios


def parse_question(soup: BeautifulSoup) -> tuple[str | None, str, list[str], str, str]:
    title_el = soup.find(id="lblTitle")
    q_el = soup.find(id="lblQues")
    title = title_el.get_text(" ", strip=True) if title_el else None
    question = q_el.get_text(" ", strip=True) if q_el else ""
    options: list[str] = []
    for lab in soup.select("label.lblDist"):
        t = lab.get_text(" ", strip=True)
        if t:
            options.append(t)
    if not options:
        pairs: list[tuple[int, str]] = []
        for lab in soup.find_all("label"):
            fid = lab.get("for") or ""
            mm = re.match(r"^chkDist(\d+)$", fid, re.I)
            if not mm:
                continue
            txt = lab.get_text(" ", strip=True)
            if txt:
                pairs.append((int(mm.group(1)), txt))
        pairs.sort(key=lambda x: x[0])
        options = [t for _, t in pairs]

    form = soup.find("form", id="form1")
    mode = detect_quiz_input_mode(form) if form is not None else "radio"
    if mode == "checkbox":
        return title, question, options, "chkDist", "checkbox"
    radio_name = infer_radio_name(soup, num_options=len(options))
    return title, question, options, radio_name, "radio"


def infer_radio_name(soup: BeautifulSoup, *, num_options: int) -> str:
    radios = collect_radio_inputs(soup)
    by_name: dict[str, list[Any]] = {}
    for r in radios:
        n = r.get("name")
        if not n:
            continue
        by_name.setdefault(n, []).append(r)

    names = set(by_name)
    if len(names) == 1:
        return next(iter(names))
    if "radDist" in names:
        return "radDist"

    if len(names) > 1 and num_options > 0:
        best_name: str | None = None
        best_diff = 10**9
        for n, group in by_name.items():
            diff = abs(len(group) - num_options)
            if diff < best_diff:
                best_diff = diff
                best_name = n
        if best_name is not None and best_diff <= 1:
            return best_name

    # Some builds render distractors via script; the server still accepts classic keys.
    if soup.find(id="lblQues") is not None and soup.select("label.lblDist"):
        return "radDist"

    form = soup.find("form", id="form1")
    dbg = []
    if form is not None:
        types: dict[str, int] = {}
        for inp in form.find_all("input"):
            types[_input_type(inp) or "(empty)"] = types.get(_input_type(inp) or "(empty)", 0) + 1
        dbg.append(f"form1_input_types={types!r}")
    btn = soup.find(id="btnJudge")
    dbg.append(f"btnJudge={btn.get('value')!r}" if btn else "btnJudge=None")
    raise RuntimeError("Could not infer radio group name; " + "; ".join(dbg))


def parse_corr_indices(corr: str | None, num_options: int) -> list[int]:
    """CorrAns is a per-option mask; one or more '1' bits (MAL may have multiple correct)."""
    if not corr:
        raise ValueError("CorrAns missing after submit")
    s = corr.strip()
    if len(s) < num_options:
        s = s.ljust(num_options, "0")
    ones = [i for i, ch in enumerate(s[:num_options]) if ch == "1"]
    if not ones:
        raise ValueError(f"No correct bits in CorrAns={corr!r}")
    return ones


def parse_corr_index(corr: str | None, num_options: int) -> int:
    """Single-select quizzes: exactly one correct bit."""
    ones = parse_corr_indices(corr, num_options)
    if len(ones) != 1:
        raise ValueError(f"Unexpected CorrAns={corr!r} for {num_options} options (expected one '1')")
    return ones[0]


def detect_quiz_input_mode(form: BeautifulSoup) -> str:
    """PracMAL-style pages use chkDist* checkboxes; most others use radDist radios."""
    for inp in form.find_all("input"):
        if _input_type(inp) != "checkbox":
            continue
        name = inp.get("name") or ""
        if "chkDist" in name:
            return "checkbox"
    return "radio"


def build_answer_submit_extra(form: BeautifulSoup, pick: str, mode: str) -> dict[str, str]:
    """Fields to post for the user's chosen distractor (besides the submit button)."""
    if mode == "checkbox":
        m = re.match(r"^(?:radDist|chkDist)(\d+)$", pick.strip(), re.I)
        n = m.group(1) if m else "1"
        suffix = f"chkDist{n}"
        for inp in form.find_all("input"):
            if _input_type(inp) != "checkbox":
                continue
            nm = inp.get("name") or ""
            if nm == suffix or nm.endswith("$" + suffix):
                return {nm: inp.get("value") or "on"}
        el = form.find(id=suffix)
        if el is not None and _input_type(el) == "checkbox":
            nm = el.get("name") or suffix
            return {str(nm): el.get("value") or "on"}
        return {suffix: "on"}
    rname = resolve_radio_group_name_for_value(form, pick)
    return {rname: pick}


def find_selection_for_next_post(form: BeautifulSoup, mode: str) -> dict[str, str]:
    """Replay the current selection when clicking Next Question (graded page)."""
    out: dict[str, str] = {}
    if mode == "checkbox":
        for inp in form.find_all("input"):
            if _input_type(inp) != "checkbox":
                continue
            name = inp.get("name") or ""
            if "chkDist" not in name:
                continue
            if inp.has_attr("checked"):
                out[name] = inp.get("value") or "on"
        return out
    sel = find_checked_radio_in_form(form)
    if sel is not None and sel.get("name") and sel.get("value"):
        out[str(sel.get("name"))] = str(sel.get("value"))
    return out


def collect_module_checkbox_payload(
    soup: BeautifulSoup,
    module_mode: str,
    *,
    certain_only: list[str] | None = None,
) -> dict[str, str]:
    """
    ASP.NET validates checked module boxes. The web UI uses JavaScript to check
    boxes for All vs Major; we reproduce that here.

    For "Only Certain Systems" (certain), POST only the listed mod* keys as checked;
    unchecked modules are omitted like a browser.
    """
    all_names = [
        cb.get("name")
        for cb in soup.select("#tblModuleDisplay input[type=checkbox]")
        if cb.get("name")
    ]
    if not all_names:
        raise RuntimeError("No module checkboxes found (fleet not selected yet?)")

    if module_mode == "all":
        return {name: "on" for name in all_names}

    if module_mode == "major":
        hid = soup.find(id="hidMajorModules")
        raw = (hid.get("value") or "") if hid else ""
        majors = [x.strip() for x in raw.split(",") if x.strip()]
        if not majors:
            raise RuntimeError("hidMajorModules empty; cannot build Major Systems payload")
        payload: dict[str, str] = {}
        for name in all_names:
            if name in majors:
                payload[name] = "on"
        return payload

    if module_mode == "certain":
        if not certain_only:
            raise RuntimeError("module_mode 'certain' requires a non-empty certain_only list")
        allowed = set(all_names)
        payload: dict[str, str] = {}
        for mid in certain_only:
            if not re.match(r"^mod\d+$", mid, re.I):
                raise ValueError(f"Invalid module id {mid!r} (expected modNNN)")
            found: str | None = None
            for a in allowed:
                if a.lower() == mid.lower():
                    found = a
                    break
            if not found:
                sample = ", ".join(sorted(allowed)[:12])
                raise ValueError(f"Unknown module id {mid!r}. Known (sample): {sample}")
            payload[found] = "on"
        return payload

    raise ValueError(f"Unknown module_mode: {module_mode!r}")


def start_params_session(session: requests.Session) -> BeautifulSoup:
    r = session.get(urljoin(BASE, "aapParams.aspx"), timeout=60)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def post_fleet_change(
    session: requests.Session, soup: BeautifulSoup, fleet_value: str
) -> BeautifulSoup:
    form = soup.find("form", id="form1")
    if not form:
        raise RuntimeError("aapParams.aspx: form1 not found")
    data = dict(asp_hidden_fields(form))
    data["__EVENTTARGET"] = "ddlFleets"
    data["__EVENTARGUMENT"] = ""
    data["ddlFleets"] = fleet_value
    r = session.post(urljoin(BASE, "aapParams.aspx"), data=data, timeout=60)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def needs_discrepancy_continue(soup: BeautifulSoup) -> bool:
    """Fewer questions available than requested — site shows lblDiscrepancy and needs btnYes=Continue."""
    lbl = soup.find(id="lblDiscrepancy")
    if lbl and lbl.get_text(strip=True):
        return True
    div = soup.find(id="divDecision2012")
    if div is None:
        return False
    st = (div.get("style") or "").replace(" ", "").lower()
    return "display:block" in st


def build_aap_params_continue_payload(
    soup: BeautifulSoup,
    *,
    fleet_value: str,
    question_limit: int,
    module_options: str,
    module_mode: str,
    certain_only: list[str] | None,
    submit_field: str,
) -> dict[str, str]:
    form = soup.find("form", id="form1")
    if not form:
        raise RuntimeError("aapParams.aspx: form1 not found after fleet select")
    data = dict(asp_hidden_fields(form))
    data["txtQuesLimit"] = str(question_limit)
    data["ModuleOptions"] = module_options
    data["ddlFleets"] = fleet_value
    data.update(
        collect_module_checkbox_payload(
            soup, module_mode, certain_only=certain_only if module_mode == "certain" else None
        )
    )
    if submit_field == "btnContinue":
        data["btnContinue"] = "Continue"
    elif submit_field == "btnYes":
        data["btnYes"] = "Continue"
    else:
        raise ValueError(f"Unknown submit_field: {submit_field!r}")
    return data


def post_continue_to_quiz(
    session: requests.Session,
    soup: BeautifulSoup,
    *,
    fleet_value: str,
    question_limit: int,
    module_options: str,
    module_mode: str,
    certain_only: list[str] | None = None,
) -> requests.Response:
    params_url = urljoin(BASE, "aapParams.aspx")
    data = build_aap_params_continue_payload(
        soup,
        fleet_value=fleet_value,
        question_limit=question_limit,
        module_options=module_options,
        module_mode=module_mode,
        certain_only=certain_only,
        submit_field="btnContinue",
    )
    r = session.post(params_url, data=data, allow_redirects=True, timeout=120)
    r.raise_for_status()
    if "aapParams.aspx" in r.url and needs_discrepancy_continue(BeautifulSoup(r.text, "lxml")):
        soup2 = BeautifulSoup(r.text, "lxml")
        data2 = build_aap_params_continue_payload(
            soup2,
            fleet_value=fleet_value,
            question_limit=question_limit,
            module_options=module_options,
            module_mode=module_mode,
            certain_only=certain_only,
            submit_field="btnYes",
        )
        r = session.post(params_url, data=data2, allow_redirects=True, timeout=120)
        r.raise_for_status()
    return r


def quiz_context_from_response(session: requests.Session, r: requests.Response) -> QuizContext:
    final_url = r.url
    soup = BeautifulSoup(r.text, "lxml")
    form = soup.find("form", id="form1")
    if not form:
        raise RuntimeError("Quiz form not found after Continue (wrong page?)")
    action = form.get("action") or ""
    quiz_url = urljoin(final_url, action)
    if not re.search(r"/PracM[A-Za-z]+\.aspx", quiz_url, re.I):
        err = soup.find(id="lblErrMsg")
        msg = err.get_text(" ", strip=True) if err else soup.get_text(" ", strip=True)[:500]
        raise RuntimeError(f"Expected a PracM*.aspx quiz page, got {quiz_url}. Server said: {msg}")
    return QuizContext(session=session, quiz_url=quiz_url)


def post_quiz_form(ctx: QuizContext, soup: BeautifulSoup, extra: dict[str, str]) -> BeautifulSoup:
    form = soup.find("form", id="form1")
    if not form:
        raise RuntimeError("Quiz form1 not found")
    data = compose_quiz_post_data(form, extra)
    r = ctx.session.post(ctx.quiz_url, data=data, timeout=120, allow_redirects=True)
    r.raise_for_status()
    ctx.last_response_url = r.url
    out = BeautifulSoup(r.text, "lxml")
    form2 = out.find("form", id="form1")
    if form2 is not None:
        act = form2.get("action") or ""
        ctx.quiz_url = urljoin(r.url, act)
    return out


def extract_graded(
    soup: BeautifulSoup,
    title_before: str | None,
    question_before: str,
    options_before: list[str],
    submitted_value: str,
    radio_name: str,
    *,
    response_url: str = "",
) -> dict[str, Any]:
    corr_el = soup.find(id="CorrAns") or soup.find("input", attrs={"name": "CorrAns"})
    corr: str | None = None
    if corr_el is not None:
        v = corr_el.get("value")
        corr = v if isinstance(v, str) else None
    inst_el = soup.find(id="lbl_Instruction")
    instruction = inst_el.get_text(" ", strip=True) if inst_el else ""
    try:
        indices = parse_corr_indices(corr, len(options_before))
    except ValueError as e:
        btn = find_toolbar_submit(soup)
        title_el = soup.find("title")
        title_txt = title_el.get_text(" ", strip=True) if title_el else ""
        hint = (
            f"{e}; response_url={response_url!r}; page_title={title_txt!r}; "
            f"toolbar_submit={repr((btn.get('name'), btn.get('value')) if btn else None)}; "
            f"has_form1={soup.find('form', id='form1') is not None}; "
            f"has_lblQues={soup.find(id='lblQues') is not None}. "
            "If this is PracMAL (checkbox distractors), ensure chkDist* fields are posted, not radDist."
        )
        raise RuntimeError(hint) from e
    idx0 = indices[0]
    correct_text = (
        options_before[idx0]
        if len(indices) == 1
        else " | ".join(options_before[i] for i in indices)
    )
    title_el = soup.find(id="lblTitle")
    title_after = title_el.get_text(" ", strip=True) if title_el else None
    qn, qtotal = parse_title_numbers(title_after or title_before)
    return {
        "title": title_after or title_before,
        "question": question_before,
        "options": options_before,
        "correct_index": idx0,
        "correct_indices": indices,
        "correct_text": correct_text,
        "corr_ans_code": corr,
        "submitted_choice": submitted_value,
        "radio_name": radio_name,
        "graded_result": instruction,
        "question_num": qn,
        "question_total": qtotal,
    }


def list_module_checkbox_ids(soup: BeautifulSoup) -> list[str]:
    """All modNNN ids from tblModuleDisplay (after fleet is selected)."""
    names: list[str] = []
    for cb in soup.select("#tblModuleDisplay input[type=checkbox]"):
        n = cb.get("name") or ""
        if re.match(r"^mod\d+$", n, re.I):
            names.append(n)
    names.sort(key=lambda x: int(x[3:], 10))
    return names


def module_label_from_params(soup: BeautifulSoup, mod_id: str) -> str:
    """Human label from tblModuleDisplay (e.g. mod000 -> '000, Aircraft General')."""
    mid = mod_id.strip()
    lab = soup.find("label", attrs={"for": mid}) or soup.find("label", attrs={"for": mid.lower()})
    if lab is not None:
        t = lab.get_text(" ", strip=True)
        if t:
            return t
    return mid


def quiz_banner_title(rows: list[dict[str, Any]]) -> str:
    """Strip '(Question N of M)' for logs — show system quiz title only."""
    for row in rows:
        t = row.get("title")
        if isinstance(t, str) and t.strip():
            return re.sub(r"\s*\(Question\s+.*$", "", t, flags=re.I).strip() or t.strip()
    return "Systems quiz"


def pair_already_stored(conn: sqlite3.Connection, question_text: str, correct_text: str) -> bool:
    """
    Skip if this question is already in question_bank (unique per question_text),
    or the same (question_text, correct_text) exists in quiz_items only.
    """
    if conn.execute(
        "SELECT 1 FROM question_bank WHERE question_text = ?",
        (question_text,),
    ).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM quiz_items WHERE question_text = ? AND correct_text = ?",
        (question_text, correct_text),
    ).fetchone():
        return True
    return False


def filter_rows_not_yet_stored(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Drop rows whose (question_text, correct_text) pair is already in the DB."""
    fresh: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if pair_already_stored(conn, row["question"], row["correct_text"]):
            skipped += 1
            continue
        fresh.append(row)
    return fresh, skipped


def params_soup_after_fleet(session: requests.Session, fleet_key: str) -> BeautifulSoup:
    """aapParams.aspx after fleet postback (module table visible)."""
    soup = start_params_session(session)
    return post_fleet_change(session, soup, FLEETS[fleet_key])


def scrape_one_run(
    session: requests.Session,
    *,
    fleet_key: str,
    question_limit: int,
    module_options: str,
    module_mode: str,
    submitted_radio_value: str,
    delay_s: float,
    certain_module_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    fleet_value = FLEETS[fleet_key]
    soup = start_params_session(session)
    soup = post_fleet_change(session, soup, fleet_value)
    if module_mode == "certain":
        if not certain_module_ids:
            raise RuntimeError("scrape_one_run: module_mode 'certain' requires certain_module_ids")
    r = post_continue_to_quiz(
        session,
        soup,
        fleet_value=fleet_value,
        question_limit=question_limit,
        module_options=module_options,
        module_mode=module_mode,
        certain_only=certain_module_ids if module_mode == "certain" else None,
    )
    ctx = quiz_context_from_response(session, r)
    soup = BeautifulSoup(r.text, "lxml")

    rows: list[dict[str, Any]] = []
    radio_name = "radDist"
    input_mode = "radio"
    while True:
        btn = find_toolbar_submit(soup)
        if not btn:
            break
        label = (btn.get("value") or "").strip()
        if label == "Next Question":
            form = soup.find("form", id="form1")
            if form is None:
                raise RuntimeError("Next Question: form1 missing")
            extra = {"btnJudge": "Next Question"}
            extra.update(find_selection_for_next_post(form, input_mode))
            if delay_s:
                time.sleep(delay_s)
            soup = post_quiz_form(ctx, soup, extra)
            continue
        if label == "See Results":
            break
        if label != "Submit Answer":
            raise RuntimeError(f"Unexpected toolbar button: {label!r}")

        title, question, options, radio_name, input_mode = parse_question(soup)
        if not question or not options:
            raise RuntimeError("Missing question or options on Submit Answer page")

        form = soup.find("form", id="form1")
        if form is None:
            raise RuntimeError("Submit Answer: form1 missing")
        answer_extra = build_answer_submit_extra(form, submitted_radio_value, input_mode)

        if delay_s:
            time.sleep(delay_s)
        soup = post_quiz_form(
            ctx,
            soup,
            {**answer_extra, "btnJudge": "Submit Answer"},
        )

        row = extract_graded(
            soup,
            title_before=title,
            question_before=question,
            options_before=options,
            submitted_value=submitted_radio_value,
            radio_name=radio_name,
            response_url=ctx.last_response_url or ctx.quiz_url,
        )
        rows.append(row)

        btn2 = find_toolbar_submit(soup)
        lab2 = (btn2.get("value") if btn2 else "") or ""
        lab2 = lab2.strip()
        if lab2 == "See Results":
            break
        if lab2 != "Next Question":
            raise RuntimeError(f"After grading, expected Next/See Results, got {lab2!r}")

        form2 = soup.find("form", id="form1")
        if form2 is None:
            raise RuntimeError("After grading: form1 missing")
        extra2: dict[str, str] = {"btnJudge": "Next Question"}
        extra2.update(find_selection_for_next_post(form2, input_mode))
        if delay_s:
            time.sleep(delay_s)
        soup = post_quiz_form(ctx, soup, extra2)

    return rows


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            fleet_key TEXT NOT NULL,
            fleet_value TEXT NOT NULL,
            question_limit INTEGER NOT NULL,
            module_options TEXT NOT NULL,
            num_questions INTEGER,
            module_scope TEXT
        );
        CREATE TABLE IF NOT EXISTS quiz_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            title TEXT,
            question_num INTEGER,
            question_total INTEGER,
            question_text TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            correct_text TEXT NOT NULL,
            corr_ans_code TEXT,
            submitted_choice TEXT,
            graded_result TEXT,
            UNIQUE(run_id, seq),
            FOREIGN KEY (run_id) REFERENCES scrape_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_quiz_items_qtext ON quiz_items(question_text);
        CREATE TABLE IF NOT EXISTS question_bank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_text TEXT NOT NULL UNIQUE,
            options_json TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            correct_text TEXT NOT NULL,
            corr_ans_code TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_question_bank_last_seen ON question_bank(last_seen_at);
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scrape_runs)").fetchall()}
    if cols and "module_scope" not in cols:
        conn.execute("ALTER TABLE scrape_runs ADD COLUMN module_scope TEXT")
    conn.commit()


def insert_run(
    conn: sqlite3.Connection,
    *,
    fleet_key: str,
    fleet_value: str,
    question_limit: int,
    module_options: str,
    num_questions: int | None,
    module_scope: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO scrape_runs (created_at, fleet_key, fleet_value, question_limit, module_options, num_questions, module_scope)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            fleet_key,
            fleet_value,
            question_limit,
            module_options,
            num_questions,
            module_scope,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_unique_questions(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """
    Insert new rows into question_bank only. No UPDATE on duplicates.
    Caller should pass rows not already in question_bank (typically after filter_rows_not_yet_stored).
    """
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    skipped = 0
    for row in rows:
        qtext = row["question"]
        ct = row["correct_text"]
        if conn.execute(
            "SELECT 1 FROM question_bank WHERE question_text = ?",
            (qtext,),
        ).fetchone():
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO question_bank (
                question_text, options_json, correct_index, correct_text, corr_ans_code,
                first_seen_at, last_seen_at, seen_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                qtext,
                json.dumps(row["options"], ensure_ascii=False),
                row["correct_index"],
                ct,
                row.get("corr_ans_code"),
                now,
                now,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted, skipped


def insert_items(conn: sqlite3.Connection, run_id: int, rows: list[dict[str, Any]]) -> None:
    for seq, row in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO quiz_items (
                run_id, seq, title, question_num, question_total, question_text, options_json,
                correct_index, correct_text, corr_ans_code, submitted_choice, graded_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                row.get("title"),
                row.get("question_num"),
                row.get("question_total"),
                row["question"],
                json.dumps(row["options"], ensure_ascii=False),
                row["correct_index"],
                row["correct_text"],
                row.get("corr_ans_code"),
                row.get("submitted_choice"),
                row.get("graded_result"),
            ),
        )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Scrape AA Systems Practice Quiz into SQLite.")
    p.add_argument("--db", default="quiz_bank.sqlite", help="SQLite database path")
    p.add_argument("--runs", type=int, default=1, help="How many full quizzes to scrape from the start")
    p.add_argument(
        "--fleet",
        default="A32F",
        choices=sorted(FLEETS.keys()),
        help="Fleet dropdown value (default A32F)",
    )
    p.add_argument("--limit", type=int, default=100, help="txtQuesLimit (requested question count)")
    p.add_argument(
        "--module-mode",
        default="all",
        choices=("all", "major", "certain"),
        help="All Systems (default), Major Systems, or Only Certain Systems (needs --each-module or --modules)",
    )
    mod_grp = p.add_mutually_exclusive_group()
    mod_grp.add_argument(
        "--each-module",
        action="store_true",
        help='With --module-mode certain: run one quiz per mod* row in tblModuleDisplay (only that module checked)',
    )
    mod_grp.add_argument(
        "--modules",
        metavar="LIST",
        help="With --module-mode certain: comma-separated mod ids (e.g. mod000,mod210); one quiz run each, only that module checked",
    )
    p.add_argument(
        "--cookies",
        help="Netscape cookies.txt from a logged-in browser (optional)",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=1,
        help="Seconds to sleep between quiz POSTs (default 1)",
    )
    p.add_argument(
        "--pick",
        default="radDist1",
        help='Radio "value" to submit each time (default radDist1); any choice works because CorrAns reveals the key',
    )
    args = p.parse_args(argv)

    module_map = {
        "all": "rbAllModuleOption",
        "major": "rbMajorModules",
        "certain": "rbSelectModulesOption",
    }
    module_options = module_map[args.module_mode]

    if args.module_mode == "certain" and not args.each_module and not args.modules:
        p.error("--module-mode certain requires --each-module or --modules mod000,mod210,...")

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    if args.cookies:
        load_mozilla_cookies(session, args.cookies)

    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        for run_idx in range(1, args.runs + 1):
            if args.module_mode == "certain":
                soup_params = params_soup_after_fleet(session, args.fleet)
                if args.each_module:
                    mod_list = list_module_checkbox_ids(soup_params)
                else:
                    raw = args.modules or ""
                    mod_list = [x.strip() for x in raw.split(",") if x.strip()]
                    if not mod_list:
                        raise RuntimeError("--modules is empty")
                    for mod in mod_list:
                        if not re.match(r"^mod\d+$", mod, re.I):
                            raise ValueError(f"Invalid --modules entry {mod!r} (expected modNNN)")
                if not mod_list:
                    raise RuntimeError("No modules to scrape (empty list)")
                print(
                    f"Run {run_idx}/{args.runs}: certain-systems, {len(mod_list)} module quiz(es) "
                    f"(limit={args.limit})",
                    flush=True,
                )
                for mod in mod_list:
                    label = module_label_from_params(soup_params, mod)
                    print(f"  {label} ...", flush=True)
                    rows = scrape_one_run(
                        session,
                        fleet_key=args.fleet,
                        question_limit=args.limit,
                        module_options=module_options,
                        module_mode="certain",
                        submitted_radio_value=args.pick,
                        delay_s=args.delay,
                        certain_module_ids=[mod],
                    )
                    new_rows, n_skip_pre = filter_rows_not_yet_stored(conn, rows)
                    title = quiz_banner_title(rows)
                    if not new_rows:
                        print(
                            f"    {title} — skip DB: all {len(rows)} question/answer pair(s) already stored",
                            flush=True,
                        )
                        continue
                    run_id = insert_run(
                        conn,
                        fleet_key=args.fleet,
                        fleet_value=FLEETS[args.fleet],
                        question_limit=args.limit,
                        module_options=module_options,
                        num_questions=len(new_rows),
                        module_scope=mod,
                    )
                    new_bank, _ = record_unique_questions(conn, new_rows)
                    insert_items(conn, run_id, new_rows)
                    print(
                        f"    {title} — saved {len(new_rows)} quiz row(s); "
                        f"question_bank +{new_bank} new; "
                        f"skipped {n_skip_pre} pair(s) already in DB before insert",
                        flush=True,
                    )
                continue

            print(
                f"Run {run_idx}/{args.runs}: starting {args.fleet} limit={args.limit} mode={args.module_mode}",
                flush=True,
            )
            rows = scrape_one_run(
                session,
                fleet_key=args.fleet,
                question_limit=args.limit,
                module_options=module_options,
                module_mode=args.module_mode,
                submitted_radio_value=args.pick,
                delay_s=args.delay,
            )
            new_rows, n_skip_pre = filter_rows_not_yet_stored(conn, rows)
            title = quiz_banner_title(rows)
            if not new_rows:
                print(
                    f"  {title} — skip DB: all {len(rows)} question/answer pair(s) already stored",
                    flush=True,
                )
                continue
            run_id = insert_run(
                conn,
                fleet_key=args.fleet,
                fleet_value=FLEETS[args.fleet],
                question_limit=args.limit,
                module_options=module_options,
                num_questions=len(new_rows),
                module_scope=None,
            )
            new_bank, _ = record_unique_questions(conn, new_rows)
            insert_items(conn, run_id, new_rows)
            print(
                f"  {title} — saved {len(new_rows)} quiz row(s); "
                f"question_bank +{new_bank} new; "
                f"skipped {n_skip_pre} pair(s) already in DB before insert",
                flush=True,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
