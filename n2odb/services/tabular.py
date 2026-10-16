"""Tabular data parsing for the upload workbench (Plane A: time-series data).

Turns an uploaded CSV/XLSX into a **source-agnostic normalized structure** — the
``parsed_content`` contract below. Everything downstream in the wizard (structure
detection, column mapping, unit conversion, review, promote) reads *only* this
structure, never the original file.

That decoupling is deliberate and load-bearing: it means the data source is
pluggable. Today the only producer is ``parse_tabular`` (file upload). A future
"extract time-series from a figure" feature would be a *different producer* that
emits the same ``ParsedDataset`` — and nothing downstream would need to change.
Keep this contract stable and keep parsing logic here; do not let downstream code
reach back to the raw bytes.

``parsed_content`` shape (JSON-serialisable)::

    {
      "source": "file",              # discriminator; future producers: "figure"
      "source_name": "<filename>",
      "sheets": [
        {
          "name":        "<sheet name>",
          "headers":     ["date", "n2o_flux", ...],
          "rows":        [["2018-05-01", "1.2", ...], ...],   # data rows, as text
          "dtypes":      ["date", "float", ...],              # inferred per column
          "n_rows":      412,          # total data rows detected
          "n_rows_stored": 412,        # may be capped by WORKBENCH_DATA_MAX_ROWS
          "header_row":  0,            # 0-based index of the header in the raw grid
          "truncated":   false
        }
      ]
    }

Parsing never raises into the request: on failure it returns a ``ParsedDataset``
with ``error`` set and no sheets.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from dataclasses import dataclass, field

from django.conf import settings

# Cells that mean "empty" for dtype inference.
_NULLISH = {"", "na", "nan", "none", "null", "-", "—"}
# Date formats we recognise when inferring a column's dtype.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y")
# How many leading rows to consider when guessing which one is the header.
_HEADER_SCAN_ROWS = 15


@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[list]
    dtypes: list[str]
    n_rows: int
    n_rows_stored: int
    header_row: int
    truncated: bool

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "headers": self.headers,
            "rows": self.rows,
            "dtypes": self.dtypes,
            "n_rows": self.n_rows,
            "n_rows_stored": self.n_rows_stored,
            "header_row": self.header_row,
            "truncated": self.truncated,
        }


@dataclass
class ParsedDataset:
    source: str = "file"
    source_name: str = ""
    sheets: list[Sheet] = field(default_factory=list)
    error: str = ""

    def as_content(self) -> dict:
        return {
            "source": self.source,
            "source_name": self.source_name,
            "sheets": [s.as_dict() for s in self.sheets],
        }


class DataTooLarge(Exception):
    pass


def _max_mb() -> int:
    return int(getattr(settings, "WORKBENCH_DATA_MAX_MB", 16))


def _max_rows() -> int:
    return int(getattr(settings, "WORKBENCH_DATA_MAX_ROWS", 50000))


# --- dtype inference ---------------------------------------------------------

def _is_nullish(v) -> bool:
    return v is None or str(v).strip().lower() in _NULLISH


def _looks_int(v: str) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == int(f) and "." not in str(v) and "e" not in str(v).lower()


def _looks_float(v: str) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _looks_date(v: str) -> bool:
    from datetime import datetime

    s = str(v).strip()
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


_BOOLISH = {"true", "false", "yes", "no", "0", "1", "0.0", "1.0"}


def _infer_dtype(values: list) -> str:
    """Infer a column dtype from its (non-null) sample values."""
    vals = [str(v).strip() for v in values if not _is_nullish(v)]
    if not vals:
        return "string"
    if all(_looks_int(v) for v in vals):
        return "int"
    if all(_looks_float(v) for v in vals):
        return "float"
    if all(_looks_date(v) for v in vals):
        return "date"
    if all(v.lower() in _BOOLISH for v in vals):
        return "bool"
    return "string"


# --- header detection --------------------------------------------------------

def _text_ratio(row: list) -> float:
    """Fraction of a row's non-empty cells that are non-numeric text."""
    cells = [c for c in row if not _is_nullish(c)]
    if not cells:
        return 0.0
    text = sum(1 for c in cells if not _looks_float(str(c)))
    return text / len(cells)


def _detect_header_row(grid: list[list]) -> int:
    """Pick the row that best looks like a header: mostly text, with the rows
    below it more numeric. A header must have data rows beneath it (so a trailing
    text row can't win). Falls back to the first non-empty row."""
    best_row, best_score = None, None
    limit = min(len(grid), _HEADER_SCAN_ROWS)
    for i in range(limit):
        if all(_is_nullish(c) for c in grid[i]):
            continue
        below = grid[i + 1:i + 6]
        if not below:
            continue  # a header can't be the last row
        row_text = _text_ratio(grid[i])
        if row_text < 0.5:
            continue  # not text-y enough to be a header
        below_text = sum(_text_ratio(r) for r in below) / len(below)
        # High text here, lower text (more numbers) below → likely header.
        score = row_text - below_text
        if best_score is None or score > best_score:
            best_row, best_score = i, score
    if best_row is not None:
        return best_row
    for i, row in enumerate(grid):  # fallback: first non-empty row
        if not all(_is_nullish(c) for c in row):
            return i
    return 0


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for idx, h in enumerate(headers):
        name = (str(h).strip() if not _is_nullish(h) else "") or f"column_{idx + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


# --- grid → Sheet ------------------------------------------------------------

def _sheet_from_grid(name: str, grid: list[list]) -> Sheet | None:
    grid = [row for row in grid if any(not _is_nullish(c) for c in row)]
    if not grid:
        return None
    header_row = _detect_header_row(grid)
    headers = _dedupe_headers(grid[header_row])
    data = grid[header_row + 1:]
    width = len(headers)
    # Pad/truncate each data row to the header width.
    norm_rows = [[(row[i] if i < len(row) else None) for i in range(width)] for row in data]

    n_rows = len(norm_rows)
    cap = _max_rows()
    stored = norm_rows[:cap]
    dtypes = [
        _infer_dtype([r[c] for r in stored]) for c in range(width)
    ]
    return Sheet(
        name=name,
        headers=headers,
        rows=stored,
        dtypes=dtypes,
        n_rows=n_rows,
        n_rows_stored=len(stored),
        header_row=header_row,
        truncated=n_rows > len(stored),
    )


# --- format parsers ----------------------------------------------------------

def _decode_csv(data: bytes) -> str:
    """Decode CSV bytes, sniffing the encoding rather than assuming UTF-8."""
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return data.decode("utf-8", "replace")


def _parse_csv(data: bytes, name: str) -> list[Sheet]:
    text = _decode_csv(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # default to comma
    grid = list(csv.reader(io.StringIO(text), dialect))
    sheet = _sheet_from_grid(name, grid)
    return [sheet] if sheet else []


def _cell_str(c) -> str:
    """Coerce an openpyxl cell to a string so xlsx yields the same all-strings
    grid the CSV path does. Native date/datetime/time cells become ISO strings
    (the date parser auto-detects ISO); otherwise ``str``. Without this, real
    Excel date cells reach the JSONField as datetime objects and 500 on save."""
    if c is None:
        return ""
    if isinstance(c, (datetime, date, time)):
        return c.isoformat()
    return str(c)


def _parse_xlsx(data: bytes) -> list[Sheet]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        grid = [
            [_cell_str(c) for c in row]
            for row in ws.iter_rows(values_only=True)
        ]
        sheet = _sheet_from_grid(ws.title, grid)
        if sheet:
            sheets.append(sheet)
    wb.close()
    return sheets


_CSV_EXTS = (".csv", ".tsv", ".txt")
_XLSX_EXTS = (".xlsx", ".xlsm")


def sniff_kind(filename: str, head: bytes) -> str | None:
    """Return "csv" or "xlsx", primarily by extension (safer than sniffing here).

    XLSX and DOCX are both zips, so we only accept a zip as XLSX when the name
    says so. A PDF or other binary blob is rejected outright.
    """
    name = (filename or "").lower()
    if name.endswith(_XLSX_EXTS):
        return "xlsx"
    if name.endswith(_CSV_EXTS):
        return "csv"
    # Unknown/missing extension: accept only if it's plainly text (not a PDF,
    # a zip/office doc, or other binary).
    if head.startswith(b"%PDF") or head.startswith(b"PK\x03\x04"):
        return None
    if head.strip() and b"\x00" not in head:
        return "csv"
    return None


def parse_tabular(kind: str, data: bytes, *, filename: str = "") -> ParsedDataset:
    """Parse an uploaded data file into the normalized ``ParsedDataset``. Never raises."""
    ds = ParsedDataset(source="file", source_name=filename)
    try:
        if len(data) > _max_mb() * 1024 * 1024:
            raise DataTooLarge(f"File exceeds the {_max_mb()} MB limit")
        if kind == "csv":
            ds.sheets = _parse_csv(data, filename or "data")
        elif kind == "xlsx":
            ds.sheets = _parse_xlsx(data)
        else:
            raise ValueError(f"Unsupported data file kind: {kind!r}")
        if not ds.sheets:
            ds.error = "No tabular data found in the file."
    except DataTooLarge as exc:
        ds.error = str(exc)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never 500
        ds.error = f"Could not read the data file: {exc}"
    return ds
