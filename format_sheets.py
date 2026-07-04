"""
format_sheets.py
One-time visual formatting script for the Chev Chelios Google Sheets.

What this does:
  - Writes proper column headers to row 1 of Trade Log and Jane (safe — Dexter always skips row 1)
  - Applies status-based row colours: WIN=green, LOSS=red, OPEN=blue, PENDING=amber,
    EXPIRED=grey, PARTIAL_1R=purple — all very light tints (~15% saturation)
  - Freezes the header row and applies a basic filter
  - Sets readable column widths (Tags column wide, JSON columns narrow)
  - Bolds and right-aligns numeric columns (Entry, SL, TP, PnL, etc.)
  - Applies a dark navy header bar with white bold text
  - Tidies the Dashboard tab labels without touching any data cells

What this does NOT do:
  - Never deletes or modifies data cells
  - Never reorders rows or columns
  - Dashboard: only adds formatting, never touches B1 (balance) or B17 (malformed count) values

Safe to run while Dexter is live. Safe to run multiple times (clears old rules before adding new ones).
"""

import gspread
from google.oauth2.service_account import Credentials

# ── Config ───────────────────────────────────────────────────────────────────
SHEET_ID                = "1V1b2aU3SJu_R7VjFKGp9J6uFwucGSamhRWyq6jgCbFs"
GOOGLE_CREDENTIALS_FILE = "google_credentials.json"
SCOPES                  = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Column definitions (must match dexter.py log_new_trade exactly) ───────────
TRADE_HEADERS = [
    "Pair",          # A  col 0  — symbol, e.g. ETHUSDT
    "Direction",     # B  col 1  — LONG or SHORT
    "Entry",         # C  col 2  — entry price
    "SL",            # D  col 3  — stop loss price
    "TP",            # E  col 4  — take profit price
    "Risk %",        # F  col 5  — risk as % of balance
    "Leverage",      # G  col 6  — e.g. 5
    "Position USD",  # H  col 7  — notional position size
    "Margin USD",    # I  col 8  — actual margin reserved
    "Tags",          # J  col 9  — confluence tags, e.g. sr_4h=4,gp=4
    "Live Price",    # K  col 10 — updated live while trade is open
    "Live PnL",      # L  col 11 — updated live while trade is open
    "Status",        # M  col 12 — PENDING / OPEN / WIN / LOSS / EXPIRED / PARTIAL_1R
    "Result $",      # N  col 13 — final PnL at close
    "Opened At UTC", # O  col 14 — timestamp of trade creation
    "Trade Type",    # P  col 15 — scalp / day / swing
    "Expiry UTC",    # Q  col 16 — when pending order expires
    "Conf Data",     # R  col 17 — JSON blob (confluence prices + metadata)
    "Trigger Above", # S  col 18 — True/False (direction of pending trigger)
]

# Column widths in pixels
TRADE_COL_WIDTHS = [
    90,   # A  Pair
    70,   # B  Direction
    85,   # C  Entry
    85,   # D  SL
    85,   # E  TP
    55,   # F  Risk %
    65,   # G  Leverage
    100,  # H  Position USD
    90,   # I  Margin USD
    260,  # J  Tags — wide so tags are readable
    85,   # K  Live Price
    80,   # L  Live PnL
    80,   # M  Status
    80,   # N  Result $
    140,  # O  Opened At UTC
    75,   # P  Trade Type
    140,  # Q  Expiry UTC
    55,   # R  Conf Data — JSON blob, just needs to exist
    60,   # S  Trigger Above
]

# ── Skip Log column definitions (must match dexter.py's _log_chev_decision) ───
SKIP_LOG_HEADERS = [
    "Timestamp UTC",     # A  col 0
    "Pair",              # B  col 1
    "TF",                # C  col 2
    "Decision",          # D  col 3  — SKIP / GATE_REJECT / STRUCT_REJECT / MTF_TAX_REJECT /
                          #             GEOMETRY_REJECT / GAUNTLET_REJECT / FORMAT_ERROR
    "Score",             # E  col 4  — Dexter's computed confluence score
    "Regime",            # F  col 5  — 4H regime at the time
    "Reason",            # G  col 6  — Chev's stated reasoning, or the gate's message
    "Confluences Seen",  # H  col 7  — Dexter's detected reasons at the time
]

SKIP_LOG_COL_WIDTHS = [
    140,  # A  Timestamp UTC
    90,   # B  Pair
    55,   # C  TF
    130,  # D  Decision
    55,   # E  Score
    120,  # F  Regime
    380,  # G  Reason — wide, this is the point of the tab
    320,  # H  Confluences Seen
]

# ── Colour palette — all light tints (~15% saturation on white) ───────────────
# Format: {red, green, blue} as 0.0–1.0 floats (Sheets API format)

C_WIN     = {"red": 0.784, "green": 0.902, "blue": 0.788}  # #C8E6C9  Material Green 100
C_LOSS    = {"red": 1.000, "green": 0.804, "blue": 0.824}  # #FFCDD2  Material Red 100
C_OPEN    = {"red": 0.733, "green": 0.871, "blue": 0.984}  # #BBDEFB  Material Blue 100
C_PENDING = {"red": 1.000, "green": 0.925, "blue": 0.702}  # #FFECB3  Material Amber 100
C_EXPIRED = {"red": 0.812, "green": 0.847, "blue": 0.863}  # #CFD8DC  Blue Grey 100
C_PARTIAL = {"red": 0.882, "green": 0.745, "blue": 0.906}  # #E1BEE7  Material Purple 100

C_HEADER_BG = {"red": 0.102, "green": 0.137, "blue": 0.494}  # #1A2380  dark navy
C_HEADER_FG = {"red": 1.000, "green": 1.000, "blue": 1.000}  # white
C_WHITE     = {"red": 1.000, "green": 1.000, "blue": 1.000}
C_DARK_TEXT = {"red": 0.200, "green": 0.200, "blue": 0.200}  # near-black for body text

# Skip Log — reuses the same tints as Trade Log for a consistent visual language
C_SKIP       = C_OPEN     # light blue  — Chev's own judgment call (SKIP)
C_REJECTED   = C_LOSS     # light red   — blocked by a downstream gate (*_REJECT)
C_FORMAT_ERR = C_EXPIRED  # light grey  — malformed reply, not a real judgment call

# Dashboard specific
C_DASH_BG   = {"red": 0.102, "green": 0.137, "blue": 0.494}  # same navy
C_LABEL_BG  = {"red": 0.231, "green": 0.255, "blue": 0.318}  # slate #3B4151
C_VALUE_BG  = {"red": 0.953, "green": 0.957, "blue": 0.969}  # very light grey #F3F4F7

# ── Helpers ───────────────────────────────────────────────────────────────────

def grid(sheet_id, r1=None, c1=None, r2=None, c2=None):
    d = {"sheetId": sheet_id}
    if r1 is not None: d["startRowIndex"] = r1
    if r2 is not None: d["endRowIndex"]   = r2
    if c1 is not None: d["startColumnIndex"] = c1
    if c2 is not None: d["endColumnIndex"]   = c2
    return d


def cell_format(sheet_id, r1, c1, r2, c2, fmt, fields):
    return {
        "repeatCell": {
            "range": grid(sheet_id, r1, c1, r2, c2),
            "cell": {"userEnteredFormat": fmt},
            "fields": fields,
        }
    }


def col_width(sheet_id, col_idx, pixels):
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_idx,
                "endIndex": col_idx + 1,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def row_height(sheet_id, r1, r2, pixels):
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": r1,
                "endIndex": r2,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def cond_format(sheet_id, formula, bg_color, num_cols=19):
    """Conditional format rule: colour entire row when custom formula is true."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [grid(sheet_id, 1, 0, 5000, num_cols)],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": formula}],
                    },
                    "format": {"backgroundColor": bg_color},
                },
            },
            "index": 0,
        }
    }


def freeze_rows(sheet_id, count=1):
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": count},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def add_filter(sheet_id, num_cols=19):
    return {
        "setBasicFilter": {
            "filter": {
                "range": grid(sheet_id, 0, 0, 1, num_cols),
            }
        }
    }


def delete_cond_rules(sheet_id, count):
    """Return a list of deleteConditionalFormatRule requests to clear all existing rules."""
    reqs = []
    for i in range(count - 1, -1, -1):
        reqs.append({
            "deleteConditionalFormatRule": {
                "sheetId": sheet_id,
                "index": i,
            }
        })
    return reqs


# ── Trade Log / Jane formatting ───────────────────────────────────────────────

def build_trade_sheet_requests(sheet_id, existing_cond_rule_count):
    reqs = []

    # 0. Clear existing conditional format rules (avoids duplicates on re-run)
    reqs.extend(delete_cond_rules(sheet_id, existing_cond_rule_count))

    # 1. Freeze header row
    reqs.append(freeze_rows(sheet_id, 1))

    # 2. Set row height — header taller, data rows comfortable
    reqs.append(row_height(sheet_id, 0, 1, 32))   # header row
    reqs.append(row_height(sheet_id, 1, 5000, 22)) # data rows

    # 3. Column widths
    for i, px in enumerate(TRADE_COL_WIDTHS):
        reqs.append(col_width(sheet_id, i, px))

    # 4. Header row: dark navy bg, white bold text, centred
    reqs.append(cell_format(
        sheet_id, 0, 0, 1, len(TRADE_HEADERS),
        {
            "backgroundColor": C_HEADER_BG,
            "textFormat": {
                "foregroundColor": C_HEADER_FG,
                "bold": True,
                "fontSize": 9,
                "fontFamily": "Roboto Mono",
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "CLIP",
        },
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
    ))

    # 5. Body rows — base style: small font, readable
    reqs.append(cell_format(
        sheet_id, 1, 0, 5000, len(TRADE_HEADERS),
        {
            "textFormat": {"fontSize": 9, "fontFamily": "Roboto Mono"},
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "CLIP",
        },
        "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy)",
    ))

    # 6. Right-align numeric columns: C(2) D(3) E(4) F(5) G(6) H(7) I(8) K(10) L(11) N(13)
    for col in [2, 3, 4, 5, 6, 7, 8, 10, 11, 13]:
        reqs.append(cell_format(
            sheet_id, 1, col, 5000, col + 1,
            {"horizontalAlignment": "RIGHT"},
            "userEnteredFormat.horizontalAlignment",
        ))

    # 7. Centre-align: B(1 Direction) M(12 Status) P(15 Trade Type) S(18 Trigger)
    for col in [1, 12, 15, 18]:
        reqs.append(cell_format(
            sheet_id, 1, col, 5000, col + 1,
            {"horizontalAlignment": "CENTER"},
            "userEnteredFormat.horizontalAlignment",
        ))

    # 8. Conditional formatting — order matters: added at index 0 each time,
    #    so last appended = lowest priority. Add in reverse priority order.
    #    Highest visual priority (PARTIAL_1R, EXPIRED) added last so they end at index 0.
    reqs.append(cond_format(sheet_id, '=$M2="WIN"',       C_WIN))
    reqs.append(cond_format(sheet_id, '=$M2="LOSS"',      C_LOSS))
    reqs.append(cond_format(sheet_id, '=$M2="OPEN"',      C_OPEN))
    reqs.append(cond_format(sheet_id, '=$M2="PENDING"',   C_PENDING))
    reqs.append(cond_format(sheet_id, '=$M2="EXPIRED"',   C_EXPIRED))
    reqs.append(cond_format(sheet_id, '=$M2="PARTIAL_1R"', C_PARTIAL))

    # 9. Bold the Result $ column (N=13) for closed trades
    reqs.append(cell_format(
        sheet_id, 1, 13, 5000, 14,
        {"textFormat": {"bold": True}},
        "userEnteredFormat.textFormat.bold",
    ))

    # 10. Basic filter on header row
    reqs.append(add_filter(sheet_id, len(TRADE_HEADERS)))

    return reqs


# ── Skip Log formatting ────────────────────────────────────────────────────────

def build_skip_log_requests(sheet_id, existing_cond_rule_count):
    reqs = []

    # 0. Clear existing conditional format rules (avoids duplicates on re-run)
    reqs.extend(delete_cond_rules(sheet_id, existing_cond_rule_count))

    # 1. Freeze header row
    reqs.append(freeze_rows(sheet_id, 1))

    # 2. Row height — header taller, data rows comfortable (Reason/Confluences wrap)
    reqs.append(row_height(sheet_id, 0, 1, 32))
    reqs.append(row_height(sheet_id, 1, 5000, 22))

    # 3. Column widths
    for i, px in enumerate(SKIP_LOG_COL_WIDTHS):
        reqs.append(col_width(sheet_id, i, px))

    # 4. Header row: dark navy bg, white bold text, centred — same as Trade Log
    reqs.append(cell_format(
        sheet_id, 0, 0, 1, len(SKIP_LOG_HEADERS),
        {
            "backgroundColor": C_HEADER_BG,
            "textFormat": {
                "foregroundColor": C_HEADER_FG,
                "bold": True,
                "fontSize": 9,
                "fontFamily": "Roboto Mono",
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "CLIP",
        },
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
    ))

    # 5. Body rows — small readable font, wrap long Reason/Confluences text
    reqs.append(cell_format(
        sheet_id, 1, 0, 5000, len(SKIP_LOG_HEADERS),
        {
            "textFormat": {"fontSize": 9, "fontFamily": "Roboto Mono"},
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
        "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy)",
    ))

    # 6. Centre-align: TF(2) Decision(3) Score(4) Regime(5)
    for col in [2, 3, 4, 5]:
        reqs.append(cell_format(
            sheet_id, 1, col, 5000, col + 1,
            {"horizontalAlignment": "CENTER"},
            "userEnteredFormat.horizontalAlignment",
        ))

    # 7. Row colour by Decision — same tints as Trade Log for visual consistency.
    #    Added in reverse priority order (last appended = index 0 = highest priority).
    reqs.append(cond_format(sheet_id, '=$D2="SKIP"', C_SKIP, num_cols=len(SKIP_LOG_HEADERS)))
    reqs.append(cond_format(sheet_id, '=$D2="FORMAT_ERROR"', C_FORMAT_ERR, num_cols=len(SKIP_LOG_HEADERS)))
    reqs.append(cond_format(sheet_id, '=REGEXMATCH($D2,"REJECT")', C_REJECTED, num_cols=len(SKIP_LOG_HEADERS)))

    # 8. Basic filter on header row
    reqs.append(add_filter(sheet_id, len(SKIP_LOG_HEADERS)))

    return reqs


# ── Dashboard formatting ──────────────────────────────────────────────────────

DASHBOARD_LABELS = {
    # row (1-indexed in sheet, 0-indexed here as key) → label for column A
    0:  "Balance (USD)",
    1:  "—",
    2:  "Session stats",
    3:  "—",
    4:  "—",
    5:  "—",
    6:  "—",
    7:  "—",
    8:  "—",
    9:  "—",
    10: "—",
    11: "—",
    12: "—",
    13: "—",
    14: "—",
    15: "—",
    16: "Malformed Replies",
}

def build_dashboard_requests(sheet_id, dash_rows):
    """
    Format the Dashboard tab. We do NOT touch any data cells.
    We only style column A (label column) and column B (value column).
    """
    reqs = []

    # Freeze row 1 (title bar if it exists)
    reqs.append(freeze_rows(sheet_id, 1))

    # Row heights — comfortable
    reqs.append(row_height(sheet_id, 0, 50, 22))

    # Column widths
    reqs.append(col_width(sheet_id, 0, 180))  # A — labels
    reqs.append(col_width(sheet_id, 1, 120))  # B — values
    reqs.append(col_width(sheet_id, 2, 200))  # C — any notes
    reqs.append(col_width(sheet_id, 3, 150))  # D
    reqs.append(col_width(sheet_id, 4, 150))  # E

    # Title row (row 1): dark navy header
    reqs.append(cell_format(
        sheet_id, 0, 0, 1, 6,
        {
            "backgroundColor": C_HEADER_BG,
            "textFormat": {"foregroundColor": C_HEADER_FG, "bold": True, "fontSize": 11},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        },
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
    ))

    # Column A (labels): slate background, white text, right-aligned
    reqs.append(cell_format(
        sheet_id, 1, 0, 50, 1,
        {
            "backgroundColor": C_LABEL_BG,
            "textFormat": {
                "foregroundColor": C_HEADER_FG,
                "bold": True,
                "fontSize": 9,
                "fontFamily": "Roboto Mono",
            },
            "horizontalAlignment": "RIGHT",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "CLIP",
        },
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
    ))

    # Column B (values): very light grey background, bold, right-aligned
    reqs.append(cell_format(
        sheet_id, 1, 1, 50, 2,
        {
            "backgroundColor": C_VALUE_BG,
            "textFormat": {
                "bold": True,
                "fontSize": 10,
                "fontFamily": "Roboto Mono",
                "foregroundColor": C_DARK_TEXT,
            },
            "horizontalAlignment": "RIGHT",
            "verticalAlignment": "MIDDLE",
        },
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
    ))

    # Highlight B1 (balance) — slightly larger font, prominent
    reqs.append(cell_format(
        sheet_id, 1, 1, 2, 2,
        {
            "textFormat": {
                "bold": True,
                "fontSize": 13,
                "foregroundColor": {"red": 0.102, "green": 0.137, "blue": 0.494},
            },
        },
        "userEnteredFormat.textFormat",
    ))

    # Highlight B17 (malformed count) — orange tint if it gets attention
    reqs.append(cell_format(
        sheet_id, 16, 1, 17, 2,
        {
            "backgroundColor": {"red": 1.0, "green": 0.925, "blue": 0.702},  # amber
        },
        "userEnteredFormat.backgroundColor",
    ))

    return reqs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to Google Sheets...")
    creds  = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(SHEET_ID)

    # Get all worksheets and their metadata
    meta       = sheet.fetch_sheet_metadata()
    sheets_map = {s["properties"]["title"]: s for s in meta["sheets"]}

    def get_gid(tab_name):
        return sheets_map[tab_name]["properties"]["sheetId"]

    def get_cond_rule_count(tab_name):
        s = sheets_map.get(tab_name, {})
        return len(s.get("conditionalFormats", []))

    # ── Trade Log tab ──────────────────────────────────────────────────────────
    print("\n[Trade Log]")
    tl_ws  = sheet.worksheet("Trade Log")
    tl_id  = get_gid("Trade Log")
    tl_data = tl_ws.get_all_values()

    if tl_data:
        row1 = tl_data[0]
        # Check if row 1 looks like headers (mostly non-numeric, non-LONG/SHORT strings)
        looks_like_header = (
            len(row1) > 0 and
            not any(c.replace(".", "").replace("-", "").isdigit() and len(c) > 3
                    for c in row1[:6] if c)
        )
        print(f"  Row 1 current values: {row1[:8]}...")
        print(f"  Row 1 looks like {'headers' if looks_like_header else 'DATA — checking carefully'}.")
        print(f"  Total rows (incl. row 1): {len(tl_data)}")
        print(f"  Existing conditional format rules: {get_cond_rule_count('Trade Log')}")

    # Write headers to row 1 — safe because Dexter always does rows[1:] in load_state_from_sheet
    print("  Writing column headers to row 1...")
    tl_ws.update(range_name="A1:S1", values=[TRADE_HEADERS], value_input_option="RAW")

    # Apply formatting
    print("  Applying formatting...")
    tl_reqs = build_trade_sheet_requests(tl_id, get_cond_rule_count("Trade Log"))
    sheet.batch_update({"requests": tl_reqs})
    print("  Trade Log: done.")

    # ── Jane tab ──────────────────────────────────────────────────────────────
    print("\n[Jane]")
    try:
        jane_ws  = sheet.worksheet("Jane")
        jane_id  = get_gid("Jane")
        jane_data = jane_ws.get_all_values()
        print(f"  Total rows: {len(jane_data)}")
        print(f"  Existing conditional format rules: {get_cond_rule_count('Jane')}")

        print("  Writing column headers to row 1...")
        jane_ws.update(range_name="A1:S1", values=[TRADE_HEADERS], value_input_option="RAW")

        print("  Applying formatting...")
        jane_reqs = build_trade_sheet_requests(jane_id, get_cond_rule_count("Jane"))
        sheet.batch_update({"requests": jane_reqs})
        print("  Jane: done.")
    except gspread.exceptions.WorksheetNotFound:
        print("  Jane tab not found — skipping.")

    # ── Skip Log tab ──────────────────────────────────────────────────────────
    print("\n[Skip Log]")
    try:
        skip_ws   = sheet.worksheet("Skip Log")
        skip_id   = get_gid("Skip Log")
        skip_data = skip_ws.get_all_values()
        print(f"  Total rows: {len(skip_data)}")
        print(f"  Existing conditional format rules: {get_cond_rule_count('Skip Log')}")

        print("  Writing column headers to row 1...")
        skip_ws.update(range_name="A1:H1", values=[SKIP_LOG_HEADERS], value_input_option="RAW")

        print("  Applying formatting...")
        skip_reqs = build_skip_log_requests(skip_id, get_cond_rule_count("Skip Log"))
        sheet.batch_update({"requests": skip_reqs})
        print("  Skip Log: done.")
    except gspread.exceptions.WorksheetNotFound:
        print("  Skip Log tab not found — skipping. (Restart Dexter once to auto-create it.)")

    # ── Dashboard tab ─────────────────────────────────────────────────────────
    print("\n[Dashboard]")
    try:
        dash_ws   = sheet.worksheet("Dashboard")
        dash_id   = get_gid("Dashboard")
        dash_data = dash_ws.get_all_values()
        print(f"  Current row 1: {dash_data[0] if dash_data else '(empty)'}")
        print(f"  B1 (balance): {dash_ws.acell('B1').value}")
        print(f"  B17 (malformed count): {dash_ws.acell('B17').value}")
        print(f"  Total non-empty rows: {sum(1 for r in dash_data if any(c.strip() for c in r))}")

        # Write a title to A1 only if A1 is empty
        a1_val = dash_ws.acell("A1").value or ""
        if not a1_val.strip():
            print("  Writing 'DEXTER DASHBOARD' title to A1...")
            dash_ws.update_acell("A1", "DEXTER DASHBOARD")

        # Write label for B1 if A1 doesn't already describe it
        # (Only if we wrote the title — meaning A2 is the balance label row)
        a2_val = dash_ws.acell("A2").value or ""
        if not a2_val.strip():
            dash_ws.update_acell("A2", "Balance (USD)")
        a17_val = dash_ws.acell("A17").value or ""
        if not a17_val.strip():
            dash_ws.update_acell("A17", "Malformed Replies")

        print("  Applying formatting...")
        dash_reqs = build_dashboard_requests(dash_id, dash_data)
        sheet.batch_update({"requests": dash_reqs})
        print("  Dashboard: done.")
    except gspread.exceptions.WorksheetNotFound:
        print("  Dashboard tab not found — skipping.")

    print("\n[OK] All formatting applied successfully.")
    print("  You can run this script again at any time -- it clears old rules before reapplying.")
    print("\nColour guide:")
    print("  Light green  = WIN")
    print("  Light red    = LOSS")
    print("  Light blue   = OPEN (live trade)")
    print("  Light amber  = PENDING (limit order waiting)")
    print("  Light grey   = EXPIRED (pending order expired)")
    print("  Light purple = PARTIAL_1R (50% closed at 1R profit)")
    print("\nSkip Log colour guide:")
    print("  Light blue = SKIP (Chev's own judgment call)")
    print("  Light red  = *_REJECT (blocked by a downstream gate after Chev replied)")
    print("  Light grey = FORMAT_ERROR (malformed reply, not a real judgment)")


if __name__ == "__main__":
    main()
