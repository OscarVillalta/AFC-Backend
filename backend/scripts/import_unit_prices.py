#!/usr/bin/env python3
"""
Update products.unit_price from a spreadsheet.

Expected layout (sheet "Sheet1" unless --sheet is given, header in row 1):
  - "id"    -> products.id
  - "PRICE" -> new unit_price (USD)

Every PRICE value is checked to be a number before it is written. Cells that
are not a number (for example "AFC") are skipped and listed in the report.
Blank cells are skipped too, so existing prices are never cleared.
Prices are rounded to 2 decimals. Only products whose price actually changes
are written.

Usage:
  python scripts/import_unit_prices.py --dry-run
  python scripts/import_unit_prices.py
  python scripts/import_unit_prices.py --file "path/to/file.xlsx" --sheet "Sheet1"
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import _startup  # noqa: F401
from app import create_app
from database import SessionLocal
from database.models import Product
from openpyxl import load_workbook
from sqlalchemy import select

DEFAULT_XLSX = Path(__file__).resolve().parents[2] / "pricingmanual.xlsx"
DEFAULT_SHEET = "Sheet1"
REPORT_PATH = Path(__file__).resolve().parent / "import_unit_prices_report.json"

ID_HEADER = "id"
PRICE_HEADER = "price"
LOOKUP_CHUNK_SIZE = 500


class NotANumberError(ValueError):
    """The cell has a value, but it is not a number."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update products.unit_price from a spreadsheet")
    parser.add_argument("--file", type=Path, default=DEFAULT_XLSX, help="Path to the .xlsx file")
    parser.add_argument("--sheet", type=str, default=DEFAULT_SHEET, help=f"Sheet name (default: {DEFAULT_SHEET})")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    return parser.parse_args()


def _parse_product_id(raw) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    text = str(raw).strip()
    return int(text) if text.isdigit() else None


def _parse_price(raw) -> float | None:
    """Return the price, or None for a blank cell.

    Raises NotANumberError when the cell is not a number, and ValueError for a
    number that can't be a price (negative, NaN, infinity).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise NotANumberError(f"not a number: {raw!r}")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip().replace("$", "").replace(",", "")
        if text == "":
            return None
        try:
            value = float(text)
        except ValueError:
            raise NotANumberError(f"not a number: {raw!r}") from None
    else:
        raise NotANumberError(f"not a number: {raw!r} ({type(raw).__name__})")
    if not math.isfinite(value):
        raise NotANumberError(f"not a finite number: {raw!r}")
    if value < 0:
        raise ValueError(f"negative price: {value}")
    return round(value, 2)


def read_price_rows(
    path: Path, sheet: str | None
) -> tuple[dict[int, float], list[dict], list[dict], int]:
    """Return ({product_id: price}, not_numeric_rows, problems, blank_price_count)."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet and sheet not in wb.sheetnames:
            raise SystemExit(f"Sheet '{sheet}' not found. Available sheets: {wb.sheetnames}")
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            raise SystemExit(f"Sheet '{ws.title}' is empty")

        normalized = [str(h).strip().lower() if h is not None else "" for h in header]
        missing = [h for h in (ID_HEADER, PRICE_HEADER) if h not in normalized]
        if missing:
            raise SystemExit(f"Missing column(s) {missing} in sheet '{ws.title}'. Found: {list(header)}")
        id_idx = normalized.index(ID_HEADER)
        price_idx = normalized.index(PRICE_HEADER)

        prices: dict[int, float] = {}
        conflicts: set[int] = set()
        not_numeric: list[dict] = []
        problems: list[dict] = []
        blank_prices = 0

        for row_number, row in enumerate(rows, start=2):
            raw_id = row[id_idx] if id_idx < len(row) else None
            raw_price = row[price_idx] if price_idx < len(row) else None
            if raw_id is None and raw_price is None:
                continue

            product_id = _parse_product_id(raw_id)
            if product_id is None:
                problems.append({"row": row_number, "id": raw_id, "error": "invalid product id"})
                continue

            try:
                price = _parse_price(raw_price)
            except NotANumberError:
                not_numeric.append({"row": row_number, "id": product_id, "value": raw_price})
                continue
            except ValueError as exc:
                problems.append({"row": row_number, "id": product_id, "error": str(exc)})
                continue
            if price is None:
                blank_prices += 1
                continue

            if product_id in prices and prices[product_id] != price:
                conflicts.add(product_id)
                problems.append({
                    "row": row_number,
                    "id": product_id,
                    "error": f"duplicate id with different price ({prices[product_id]} vs {price})",
                })
                continue
            prices[product_id] = price

        for product_id in conflicts:
            prices.pop(product_id, None)

        return prices, not_numeric, problems, blank_prices
    finally:
        wb.close()


def main() -> int:
    args = parse_args()
    if not args.file.exists():
        print(f"File not found: {args.file}")
        return 1

    prices, not_numeric, problems, blank_prices = read_price_rows(args.file, args.sheet)

    app = create_app()
    db = SessionLocal()

    changes: list[dict] = []
    unchanged = 0
    missing_ids: list[int] = []

    try:
        with app.app_context():
            ids = list(prices)
            products: dict[int, Product] = {}
            for start in range(0, len(ids), LOOKUP_CHUNK_SIZE):
                chunk = ids[start:start + LOOKUP_CHUNK_SIZE]
                for product in db.scalars(select(Product).where(Product.id.in_(chunk))):
                    products[product.id] = product

            for product_id, new_price in prices.items():
                product = products.get(product_id)
                if product is None:
                    missing_ids.append(product_id)
                    continue
                current = round(product.unit_price, 2) if product.unit_price is not None else None
                if current == new_price:
                    unchanged += 1
                    continue
                changes.append({
                    "product_id": product_id,
                    "unit_price_before": product.unit_price,
                    "unit_price_after": new_price,
                })
                if not args.dry_run:
                    product.unit_price = new_price

            if not args.dry_run and changes:
                db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "file": str(args.file),
        "sheet": args.sheet,
        "rows_with_numeric_price": len(prices),
        "rows_not_numeric_skipped": len(not_numeric),
        "rows_blank_price_skipped": blank_prices,
        "would_update" if args.dry_run else "updated": len(changes),
        "unchanged": unchanged,
        "product_ids_not_found": sorted(missing_ids),
        "problems": problems,
    }
    REPORT_PATH.write_text(
        json.dumps({**summary, "not_numeric": not_numeric, "changes": changes}, indent=2, default=str),
        encoding="utf-8",
    )

    print(json.dumps({k: v for k, v in summary.items() if k not in ("problems", "product_ids_not_found")}, indent=2))
    print(f"Product ids not found: {len(missing_ids)}  |  Problem rows: {len(problems)}")
    if not_numeric:
        values = sorted({str(r["value"]) for r in not_numeric})
        print(f"Skipped non-numeric PRICE values: {values[:10]}{' ...' if len(values) > 10 else ''}")
    if changes:
        print("\nSample changes:")
        for row in changes[:20]:
            print(row)
        if len(changes) > 20:
            print(f"... and {len(changes) - 20} more")
    print(f"\nFull report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
