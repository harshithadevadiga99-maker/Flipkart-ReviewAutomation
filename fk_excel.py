# v6
"""
Excel output. One workbook per product, appended per run date.

Sheets: Star Ratings | Positive Reviews | Negative Reviews | Time
Layout is computed from the data (no hard-coded row numbers that break when rows shift).
"""
import logging
import os
import re
import zipfile
from collections import Counter
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from fk_parsing import date_status_text, parse_review_date, window_state

log = logging.getLogger("fk")

GREEN = PatternFill(start_color="8DBF51", end_color="8DBF51", fill_type="solid")
YELLOW = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
THICK = Side(style="medium", color="000000")
BOX = Border(left=THICK, right=THICK, top=THICK, bottom=THICK)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
STARS = (5, 4, 3, 2, 1)
# INTERNAL row layout (what the code works with). Not every field is written to the report - see REPORT_HEADERS.
REVIEW_HEADERS = ["User Name", "Ratings", "Short Review", "Review Description", "Date",
                  "Reason", "Consolidated Reason", "Run Date",
                  "Variant", "Location", "Helpful Votes", "Verified Purchase",
                  "Date Status", "Possible Date Range", "Started At", "Source Sections"]
# Columns that appear in the Positive / Negative Reviews sheets, in this order.
# (Reason, Helpful Votes and Date Status are kept internally but are not shown in the report.)
REPORT_HEADERS = ["User Name", "Ratings", "Short Review", "Review Description", "Date",
                  "Consolidated Reason", "Run Date", "Variant", "Location", "Verified Purchase",
                  "Possible Date Range", "Started At", "Source Sections"]
_REPORT_IDX = [REVIEW_HEADERS.index(h) for h in REPORT_HEADERS]
_HEADER_ALIASES = {"Scraped At": "Started At"}   # header used by older workbooks
# 'Date' is the REVIEW date as Flipkart shows it. 'Run Date' / 'Started At' are when the scraper ran.
# 'Possible Date Range' is the earliest..latest day the review could have been written (a single day when exact).
_RANGE_COL = REVIEW_HEADERS.index("Possible Date Range")
_SUMMARY_HEADERS = ("Rating", "Aggregate count, all dates (Star Ratings)", "Individually extracted reviews in date range")


# def safe_filename(name):
#     s = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()
#     return (s or "product") + ".xlsx"def safe_filename(name):
def safe_filename(name):
    """Create a short, Windows-safe Excel filename."""
    s = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()
    s = re.sub(r"\s+", " ", s).strip(" .")

    if not s:
        s = "product"

    # Keep the filename short enough for Windows paths.
    s = s[:80].rstrip(" .")

    return (s or "product") + ".xlsx"



def _open(path):
    if os.path.exists(path):
        try:
            return load_workbook(path)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            backup = f"{path}.corrupt-{datetime.now():%Y%m%d%H%M%S}"
            os.replace(path, backup)
            log.warning("Workbook unreadable (%s); moved to %s and recreated", exc, os.path.basename(backup))
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def _sheet(wb, name):
    return wb[name] if name in wb.sheetnames else wb.create_sheet(name)


def _merge_once(ws, rng):
    if rng not in [str(r) for r in ws.merged_cells.ranges]:
        ws.merge_cells(rng)


def _banner(ws, product, subtitle, sub_row=2):
    _merge_once(ws, "A1:B1")
    c = ws["A1"]
    c.value, c.font, c.fill, c.alignment = f"Product Name: {product}", Font(size=14, bold=True), GREEN, CENTER
    _merge_once(ws, f"A{sub_row}:B{sub_row}")
    c = ws[f"A{sub_row}"]
    c.value, c.font, c.fill, c.alignment = subtitle, Font(size=13, bold=True), YELLOW, CENTER


def _num(v):
    m = re.search(r"-?\d+(?:\.\d+)?", str(v if v is not None else ""))
    return float(m.group()) if m else None


def _date_col(ws, row, header):
    """Column holding `header` in `row`, else the first free column after the last filled header."""
    last = 1
    for c in range(2, ws.max_column + 1):
        v = ws.cell(row, c).value
        if v == header:
            return c
        if v not in (None, ""):
            last = c
    return last + 1


def _style_header(ws, row, width=22):
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(row, c)
        cell.font, cell.alignment, cell.border = Font(bold=True), CENTER, BOX
        ws.column_dimensions[get_column_letter(c)].width = width


# --------------------------------------------------------------------------- #
def _write_star_sheet(wb, product, run_date, counts, overall, date_range=None):
    ws = _sheet(wb, "Star Ratings")
    _banner(ws, product, "Flipkart Rating Counts Analysis")
    ws["A3"] = ("Aggregate counts cover ALL ratings shown by Flipkart (all time), not only the review date range "
                f"{_range_text(date_range)}.")
    ws["A3"].font = Font(italic=True)
    ws["A4"] = "Product Ratings"
    for i, s in enumerate(STARS):
        ws.cell(5 + i, 1, f"Rating{s}")
    ws.cell(10, 1, "Total Positive Reviews")
    ws.cell(11, 1, "Total Negative Reviews")
    ws.cell(12, 1, "Overall Product Rating (computed from counts)")

    col = _date_col(ws, 4, run_date)
    ws.cell(4, col, run_date)
    if counts and all(k in counts for k in STARS):   # verified histogram
        for i, s in enumerate(STARS):
            ws.cell(5 + i, col, counts[s])
        ws.cell(10, col, counts[5] + counts[4])
        ws.cell(11, col, counts[3] + counts[2] + counts[1])  # fixed: was 3+2+2
        ws.cell(12, col, overall if overall is not None else "N/A")
    else:                                            # unknown is N/A, never a fake 0
        for r in range(5, 13):
            ws.cell(r, col, "N/A")

    # Change vs previous run (rebuilt every run from the data above)
    _merge_once(ws, "A14:B14")
    t = ws["A14"]
    t.value, t.font, t.fill, t.alignment = "Tracking Changes In Ratings Over Time", Font(size=14, bold=True), YELLOW, CENTER
    ws.cell(15, 1, "Product Ratings")
    last = ws.max_column
    for c in range(2, last + 1):
        ws.cell(15, c, ws.cell(4, c).value)
    for r in range(16, 23):
        ws.cell(r, 1, ws.cell(r - 11, 1).value)
        for c in range(2, last + 1):
            if c == 2:
                ws.cell(r, c, "-")
                continue
            cur, prev = _num(ws.cell(r - 11, c).value), _num(ws.cell(r - 11, c - 1).value)
            ws.cell(r, c, (cur - prev) if cur is not None and prev is not None else "-")
    _style_header(ws, 4)
    _style_header(ws, 15)


def _write_keyword_sheet(wb, product, run_date, keywords):
    ws = _sheet(wb, "Positive Negative Values")
    _banner(ws, product, "Positive and Negative Review Counts for Each Keyword")
    ws["A4"] = "Keywords"
    pos_h, neg_h = f"{run_date} Positive Reviews", f"{run_date} Negative Reviews"
    pc = _date_col(ws, 4, pos_h)
    nc = pc + 1
    ws.cell(4, pc, pos_h)
    ws.cell(4, nc, neg_h)

    rows = {}
    r = 5
    while ws.cell(r, 1).value not in (None, "") and not str(ws.cell(r, 1).value).startswith("Tracking"):
        rows[ws.cell(r, 1).value] = r
        r += 1
    next_row = r
    for k in keywords:
        row = rows.get(k["Keywords"])
        if row is None:
            row, rows[k["Keywords"]] = next_row, next_row
            next_row += 1
            ws.cell(row, 1, k["Keywords"])
        ws.cell(row, pc, k["Positive Reviews"])
        ws.cell(row, nc, k["Negative Reviews"])
    last_kw_row = next_row - 1

    # rebuild the change block below the keyword rows
    if ws.max_row > last_kw_row:
        ws.delete_rows(last_kw_row + 1, ws.max_row - last_kw_row)
    start = last_kw_row + 2
    _merge_once(ws, f"A{start}:B{start}")
    t = ws[f"A{start}"]
    t.value, t.font, t.fill, t.alignment = ("Tracking Changes In Positive and Negative Reviews Over Time",
                                            Font(size=12, bold=True), YELLOW, CENTER)
    hdr = start + 1
    ws.cell(hdr, 1, "Keywords")
    pairs = [c for c in range(2, ws.max_column + 1, 2) if ws.cell(4, c).value]
    for c in range(2, ws.max_column + 1):
        ws.cell(hdr, c, ws.cell(4, c).value)
    for i, kr in enumerate(range(5, last_kw_row + 1)):
        out = hdr + 1 + i
        ws.cell(out, 1, ws.cell(kr, 1).value)
        for idx, pcol in enumerate(pairs):
            for c in (pcol, pcol + 1):
                if idx == 0:
                    ws.cell(out, c, "-")
                else:
                    cur, prev = _num(ws.cell(kr, c).value), _num(ws.cell(kr, c - 2).value)
                    ws.cell(out, c, f"{cur - prev:+.0f}%" if cur is not None and prev is not None else "-")
    _style_header(ws, 4)
    _style_header(ws, hdr)


# Older versions of this tool padded the review sheets with fake "Rating only (no review)" rows so the
# row count matched Flipkart's aggregate star counts. Those rows are NOT customer records. The name is
# kept ONLY so that such rows are recognised and removed from existing workbooks - nothing creates them.
LEGACY_PLACEHOLDER_NAME = "Rating only (no review)"
NEGATIVE_SHEET, POSITIVE_SHEET = "Negative Reviews", "Positive Reviews"


def _range_text(date_range):
    start, end = date_range or (None, None)
    if not (start or end):
        return "(none - all dates)"
    return f"{start.isoformat() if start else 'earliest'} to {end.isoformat() if end else 'latest'} (inclusive)"


def _possible_range(rv):
    e, l = rv.get("Date Earliest") or "", rv.get("Date Latest") or ""
    return "N/A" if not e or not l else (e if e == l else f"{e} to {l}")


def _row_for(rv, run_date, scraped_at=None):
    rating = rv.get("Ratings", "N/A")
    if not isinstance(rating, int) and rv.get("Inferred Sentiment"):
        rating = f"N/A (topic text: {rv['Inferred Sentiment']})"   # no stars shown in topic lists
    return [rv.get("User Name", "N/A"), rating, rv.get("Short Review", "N/A"),
            rv.get("Review Description", "N/A"), rv.get("Date", "N/A"),
            rv.get("Reason", "Unclassified"), rv.get("Consolidated Reason", "Unclassified"),
            run_date, rv.get("Variant", "N/A"), rv.get("Location", "N/A"),
            rv.get("Helpful Votes", "N/A"), rv.get("Verified Purchase", "N/A"),
            rv.get("Date Status", "N/A"), _possible_range(rv), rv.get("Scraped At") or scraped_at or "N/A", rv.get("Source Sections") or "N/A"]


LEGACY_ROW_STATUS = "Legacy row - day may be estimated"


def _row_state(row, window):
    """Window state of a sheet row. Uses the stored 'Possible Date Range'; rows written by older
    versions (no range) fall back to their absolute 'Date' text. Relative text is never guessed."""
    start, end = window
    rng = row[_RANGE_COL] if len(row) > _RANGE_COL else None
    if rng not in (None, "", "N/A"):
        try:
            lo, _, hi = str(rng).partition(" to ")
            return window_state(date.fromisoformat(lo.strip()), date.fromisoformat((hi or lo).strip()), start, end)
        except ValueError:
            pass
    d = parse_review_date(row[4])
    if d["source"] == "relative":
        return "undated"
    return window_state(d["earliest"], d["latest"], start, end)


def _normalise_row(row):
    """Pad rows from older workbooks to the current column count (never inventing values)."""
    row = list(row) + [None] * (len(REVIEW_HEADERS) - len(row))
    if row[_RANGE_COL - 1] in (None, ""):
        row[_RANGE_COL - 1] = LEGACY_ROW_STATUS
    for i in (_RANGE_COL, _RANGE_COL + 1):
        row[i] = "N/A" if row[i] in (None, "") else row[i]
    return row


def _is_legacy_placeholder(row):
    return bool(row) and str(row[0]).strip() == LEGACY_PLACEHOLDER_NAME


def _key(row):
    # A record key supports rerun idempotence; a multiplicity counter below preserves identical
    # records within one collection instead of collapsing them into a set.
    return tuple(str(row[i] or "").strip() for i in (0, 1, 2, 3, 8, 9, 11))


def _write_review_sheet(wb, sheet_name, subtitle, product, run_date, reviews, stars, counts, legacy_rows=(),
                        window=None, scraped_at=None, include_uncertain=False, stats=None):
    """One sheet of reviews for the given star values.

    * only genuine, individually extracted reviews are written; they accumulate across runs
      (de-duplicated, never lost)
    * with a date `window` (start, end) only reviews dated inside it are listed - both new reviews and
      rows already in the workbook (rows whose day is unknown but may be inside are kept and flagged)
    * leftover 'Rating only (no review)' placeholder rows from older versions are dropped
    * the aggregate star counts stay on the 'Star Ratings' sheet; a summary table at the top of the sheet
      compares them with the number of extracted reviews but never creates rows to close the gap
    Returns (real_rows_added, legacy_placeholder_rows_removed). `stats` (dict) receives
    'out_of_range_removed' (existing rows dropped by the window) and 'new_out_of_range'."""
    window = window or (None, None)
    filtering = bool(window[0] or window[1])
    stats = stats if stats is not None else {}
    stats.update(out_of_range_removed=0, new_out_of_range=0)

    def allowed(row):
        """Keep every review that is, or may be, inside the requested date range.

        'in'        every possible day of the review is inside the range -> kept
        'uncertain' Flipkart only shows 'N months ago' and the possible range overlaps the range -> kept
                    when include_uncertain (default), and labelled in the 'Date Status' / 'Possible Date
                    Range' columns so nothing is silently dropped or silently trusted
        'before'/'after'/'undated' -> not in the range"""
        if not filtering:
            return True
        st = _row_state(row, window)
        return st == "in" or (st == "uncertain" and include_uncertain)

    idx = wb.sheetnames.index(sheet_name) if sheet_name in wb.sheetnames else None
    kept, existing_counts, removed = [], Counter(), 0

    def keep_existing(row):
        nonlocal removed
        if _is_legacy_placeholder(row):
            removed += 1
        else:
            row = _normalise_row(row)
            if allowed(row):
                kept.append(row)
                existing_counts[_key(row)] += 1
            else:
                stats["out_of_range_removed"] += 1

    if idx is not None:
        for row in _existing_rows(wb[sheet_name]):
            keep_existing(row)
        del wb[sheet_name]
    for row in legacy_rows:                      # rows migrated from the old single 'Reviews' sheet
        keep_existing(row)
    added = 0
    incoming_counts = Counter()
    for rv in reviews:
        row = _row_for(rv, run_date, scraped_at)
        key = _key(row)
        incoming_counts[key] += 1
        # Existing copies satisfy the matching multiplicity on a repeated run. Additional
        # identical records from this request are retained as separate rows.
        if incoming_counts[key] <= existing_counts[key]:
            continue
        if filtering and rv.get("Date Precision"):     # status relative to THIS workbook's range
            row[_RANGE_COL - 1] = date_status_text(rv["Date Precision"], _row_state(row, window))
        if not allowed(row):
            stats["new_out_of_range"] += 1
            continue
        kept.append(row)
        added += 1

    # Layout (computed, never hard-coded):
    #   rows 1-2   product banner / sheet title
    #   row 4+     summary table (aggregate star counts vs extracted reviews) in columns A:C, then a Total row
    #   3 blank rows, then the review table header, then the reviews
    ws = wb.create_sheet(sheet_name, idx)
    _banner(ws, product, subtitle)
    total_row = 5 + len(stars)
    head_row = total_row + 3
    first = head_row + 1
    last = max(first + len(kept) - 1, first)

    for i, h in enumerate(_SUMMARY_HEADERS):
        cell = ws.cell(4, 1 + i, h)
        cell.font, cell.alignment, cell.border = Font(bold=True), CENTER, BOX
    ws.row_dimensions[4].height = 32
    for i, s in enumerate(stars):
        r = 5 + i
        ws.cell(r, 1, s)
        ws.cell(r, 2, int(counts[s]) if counts and s in counts else "N/A")
        ws.cell(r, 3, f"=COUNTIF($B${first}:$B${last},A{r})")
    ws.cell(total_row, 1, "Total")
    ws.cell(total_row, 2, f"=SUM(B5:B{total_row - 1})")
    ws.cell(total_row, 3, f"=SUM(C5:C{total_row - 1})")
    for c in (1, 2, 3):
        ws.cell(total_row, c).font = Font(bold=True)
    ws.cell(5, 5, "This sheet lists only reviews that were individually extracted from Flipkart.").font = Font(italic=True)
    ws.cell(6, 5, "Aggregate star counts include all dates and ratings without a visible review; "
                  "no rows are created for them.").font = Font(italic=True)

    for j, h in enumerate(REPORT_HEADERS, 1):
        cell = ws.cell(head_row, j, h)
        cell.font, cell.alignment, cell.border = Font(bold=True), CENTER, BOX
    for i, row in enumerate(kept):
        for j, src in enumerate(_REPORT_IDX, 1):
            ws.cell(first + i, j, row[src])
    if removed:
        log.info("[%s] %s: removed %d placeholder 'Rating only' row(s) left by an older version",
                 product, sheet_name, removed)
    if stats["out_of_range_removed"]:
        log.info("[%s] %s: removed %d existing row(s) dated outside %s", product, sheet_name,
                 stats["out_of_range_removed"], _range_text(window))

    have = {s: sum(1 for r in kept if _num(r[1]) == s) for s in stars}
    if counts and all(s in counts for s in STARS):
        for s in stars:
            if have[s] > int(counts[s]):
                log.warning("[%s] %s: %d '%d-star' reviews collected but Flipkart reports only %d ratings",
                            product, sheet_name, have[s], s, counts[s])

    for c in range(1, len(REPORT_HEADERS) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 24
    ws.column_dimensions["D"].width = 60
    ws.column_dimensions[get_column_letter(REPORT_HEADERS.index("Possible Date Range") + 1)].width = 26
    ws.freeze_panes = f"A{first}"
    return added, removed


def _existing_rows(ws):
    """Rows already in a review sheet, returned in the INTERNAL layout (REVIEW_HEADERS).

    Columns are found by header name, so workbooks written by an older layout (16 columns, review table at
    row 4, 'Scraped At') and by the current layout (13 columns, table below the summary) are both read. Fields
    the sheet does not have (e.g. Reason, Helpful Votes, Date Status) come back empty - never invented."""
    head = next((r for r in range(1, min(ws.max_row, 40) + 1) if ws.cell(r, 1).value == "User Name"), None)
    if head is None:
        return []
    pos = {}
    for c in range(1, ws.max_column + 1):
        h = ws.cell(head, c).value
        h = _HEADER_ALIASES.get(h, h)
        if h in REVIEW_HEADERS and h not in pos:
            pos[h] = c
    rows = []
    for r in range(head + 1, ws.max_row + 1):
        if ws.cell(r, 1).value is None:
            continue
        rows.append([ws.cell(r, pos[h]).value if h in pos else None for h in REVIEW_HEADERS])
    return rows


def _legacy_negative_rows(wb):
    """Rows from the old single 'Reviews' sheet (negative only) so earlier runs are not lost."""
    if "Reviews" not in wb.sheetnames:
        return []
    rows = [list(r) for r in wb["Reviews"].iter_rows(min_row=5, max_col=len(REVIEW_HEADERS), values_only=True)
            if r and r[0] is not None]
    del wb["Reviews"]
    return rows


def _write_time_sheet(wb, date_range=None, run_info=None):
    """One row per run: when the scraper ran (NOT a review date), the review date range it used and how
    far back it could verify it reached."""
    ws = _sheet(wb, "Time")
    heads = ["Request ID", "Product Name", "Requested Duration", "Duration Unit", "Start Date", "End Date",
             "Scraping Start Time", "Scraping Completion Time", "Report Generation Time", "Total Extracted Reviews"]
    if ws.max_column > len(heads):               # older workbooks had 4 extra status columns - no longer reported
        ws.delete_cols(len(heads) + 1, ws.max_column - len(heads))
    for i, h in enumerate(heads, 1):
        if ws.cell(1, i).value != h:
            ws.cell(1, i, h)
            ws.cell(1, i).font = Font(bold=True)
            ws.column_dimensions[get_column_letter(i)].width = 26
    start, end = date_range or (None, None)
    info = run_info or {}
    stamp = info.get("scraped_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append([info.get("request_id") or "N/A", info.get("product_name", ""), info.get("duration", ""),
               info.get("duration_unit", ""), start.isoformat() if start else "N/A", end.isoformat() if end else "N/A",
               info.get("scraping_started_at", stamp), info.get("scraping_completed_at", stamp),
               info.get("report_generated_at", stamp), info.get("total_extracted_reviews", 0)])


def update_workbook(path, product, run_date, star_counts, overall, keywords, neg_reviews,
                    pos_reviews=None, stats=None, date_range=None, run_info=None, include_uncertain=False):
    """Update every sheet; one failing sheet never blocks the others. Saves atomically.
    Sheets: Star Ratings | Positive Reviews | Negative Reviews | Time.
    `date_range` = (start, end) inclusive dates: the individual review sheets then list only reviews dated
    inside it. `run_info` = {'scraped_at', 'coverage', 'stop_reason'} for the Time sheet / Scraped At column.
    Returns (problems, negative_real_rows_added); extra numbers go into `stats` if given."""
    wb = _open(path)
    problems = []
    stats = stats if stats is not None else {}
    stats.update(neg_added=0, pos_added=0, neg_placeholders_removed=0, pos_placeholders_removed=0,
                 out_of_range_removed=0, new_out_of_range=0)
    run_info = run_info or {}
    scraped_at = run_info.get("scraped_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_info = {**run_info, "scraped_at": scraped_at}

    def reviews():
        legacy = _legacy_negative_rows(wb)
        extra = {}
        stats["neg_added"], stats["neg_placeholders_removed"] = _write_review_sheet(
            wb, NEGATIVE_SHEET, "Negative Reviews (1-3 star)", product, run_date, neg_reviews,
            (3, 2, 1), star_counts, legacy, date_range, scraped_at, include_uncertain, extra)
        for k, v in extra.items():
            stats[k] += v
        extra = {}
        stats["pos_added"], stats["pos_placeholders_removed"] = _write_review_sheet(
            wb, POSITIVE_SHEET, "Positive Reviews (4-5 star)", product, run_date, pos_reviews or [],
            (5, 4), star_counts, (), date_range, scraped_at, include_uncertain, extra)
        for k, v in extra.items():
            stats[k] += v

    steps = [("Star Ratings", lambda: _write_star_sheet(wb, product, run_date, star_counts, overall, date_range))]
    steps += [("Reviews", reviews), ("Time", lambda: _write_time_sheet(wb, date_range, run_info))]
    for label, fn in steps:
        try:
            fn()
        except Exception as exc:
            problems.append(f"{label}: {exc}")
            log.exception("Sheet '%s' failed", label)
    # keep a tidy tab order
    # Remove the legacy keyword-metrics tab so current workbooks always have the requested four sheets.
    if "Positive Negative Values" in wb.sheetnames:
        del wb["Positive Negative Values"]
    order = ["Star Ratings", POSITIVE_SHEET, NEGATIVE_SHEET, "Time"]
    wb._sheets.sort(key=lambda s: order.index(s.title) if s.title in order else len(order))
    tmp = path + ".tmp"
    wb.save(tmp)
    os.replace(tmp, path)
    return problems, stats["neg_added"]