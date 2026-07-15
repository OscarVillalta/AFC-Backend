#!/usr/bin/env python3
"""
Import new air filter products from Item List.xlsm (sheet Additions).

For each row, creates an AirFilter + Product + per-warehouse Quantity records,
then commits an adjustment transaction for the initial Quantity On Hand in
warehouse 1.

Column mapping (sheet Additions):
  Item              -> part_number
  Description       -> description (+ dimensions/MERV parsed from text)
  Type              -> air_filter category name (e.g. Pleated)
  Quantity On Hand  -> initial inventory transaction amount
  Preferred Vendor  -> supplier name

Usage:
  python scripts/import_item_list_additions.py --dry-run
  python scripts/import_item_list_additions.py --dry-run --limit 5
  python scripts/import_item_list_additions.py --item ZLP15151SP
  python scripts/import_item_list_additions.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import _startup  # noqa: F401
from app import create_app
from app.services.item_list_additions_xlsx import iter_item_list_addition_rows
from database import SessionLocal
from database.models import (
    AirFilter,
    AirFilterCategory,
    Product,
    ProductCategory,
    Quantity,
    Supplier,
    Transaction,
    TransactionReason,
    TransactionState,
    Warehouse,
)
from sqlalchemy import func, select

DEFAULT_XLSX = Path(__file__).resolve().parents[2] / "Item List.xlsm"
REPORT_PATH = Path(__file__).resolve().parent / "import_item_list_additions_report.json"
DEFAULT_NOTE = "Item List Additions import"
AIR_FILTER_PRODUCT_CATEGORY = "Air Filters"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import new air filters from Item List.xlsm Additions sheet"
    )
    parser.add_argument(
        "--xlsx",
        type=Path,
        default=DEFAULT_XLSX,
        help="Path to Item List.xlsm",
    )
    parser.add_argument(
        "--warehouse-id",
        type=int,
        default=1,
        help="Warehouse for initial inventory transaction (default: 1)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--item", type=str, default=None, help="Import a single item only")
    parser.add_argument("--note", type=str, default=DEFAULT_NOTE)
    parser.add_argument(
        "--default-merv",
        type=int,
        default=10,
        help="MERV rating when not present in description (default: 10)",
    )
    return parser.parse_args()


def _resolve_supplier(db, vendor_name: str | None) -> Supplier | None:
    if not vendor_name:
        return None
    return db.execute(
        select(Supplier).where(func.lower(Supplier.name) == vendor_name.strip().lower())
    ).scalar_one_or_none()


def _resolve_air_filter_category(db, type_name: str | None) -> AirFilterCategory | None:
    if not type_name:
        return None
    return db.execute(
        select(AirFilterCategory).where(
            func.lower(AirFilterCategory.name) == type_name.strip().lower()
        )
    ).scalar_one_or_none()


def _resolve_product_category_id(db) -> int:
    category = db.execute(
        select(ProductCategory).where(ProductCategory.name == AIR_FILTER_PRODUCT_CATEGORY)
    ).scalar_one_or_none()
    if not category:
        raise RuntimeError(f"Product category '{AIR_FILTER_PRODUCT_CATEGORY}' not found.")
    return category.id


def process_row(
    db,
    row,
    *,
    warehouse_id: int,
    dry_run: bool,
    note: str,
    default_merv: int,
) -> dict:
    skip_reason = row.should_skip()
    if skip_reason:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": skip_reason,
            "row": row.row_number,
        }

    existing = db.execute(
        select(AirFilter).where(AirFilter.part_number == row.item)
    ).scalar_one_or_none()
    if existing:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": "part number already exists",
            "row": row.row_number,
            "air_filter_id": existing.id,
        }

    supplier = _resolve_supplier(db, row.preferred_vendor)
    if not supplier:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": f"supplier not found: {row.preferred_vendor}",
            "row": row.row_number,
        }

    category = _resolve_air_filter_category(db, row.filter_type)
    if not category:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": f"air filter category not found: {row.filter_type}",
            "row": row.row_number,
        }

    merv_rating = row.merv_rating if row.merv_rating > 0 else default_merv

    if dry_run:
        return {
            "item": row.item,
            "status": "dry_run",
            "row": row.row_number,
            "part_number": row.item,
            "description": row.description,
            "supplier": supplier.name,
            "category": category.name,
            "height": row.height,
            "width": row.width,
            "depth": row.depth,
            "merv_rating": merv_rating,
            "quantity_on_hand": row.quantity_on_hand,
            "warehouse_id": warehouse_id,
        }

    air_filter = AirFilter(
        part_number=row.item,
        description=row.description,
        supplier_id=supplier.id,
        category_id=category.id,
        merv_rating=merv_rating,
        height=row.height,
        width=row.width,
        depth=row.depth,
    )
    db.add(air_filter)
    db.flush()

    product = Product(
        category_id=_resolve_product_category_id(db),
        reference_id=air_filter.id,
    )
    db.add(product)
    db.flush()

    warehouses = db.execute(select(Warehouse)).scalars().all()
    qty_record = None
    for wh in warehouses:
        qty = Quantity(
            product_id=product.id,
            warehouse_id=wh.id,
            on_hand=0,
            reserved=0,
            ordered=0,
            location=0,
        )
        db.add(qty)
        if wh.id == warehouse_id:
            qty_record = qty
    db.flush()

    transaction_id = None
    if row.quantity_on_hand > 0:
        if not qty_record:
            qty_record = db.execute(
                select(Quantity).where(
                    (Quantity.product_id == product.id)
                    & (Quantity.warehouse_id == warehouse_id)
                )
            ).scalar_one_or_none()
        if not qty_record:
            raise RuntimeError(f"Quantity record missing for warehouse {warehouse_id}")

        txn = Transaction(
            product_id=product.id,
            warehouse_id=warehouse_id,
            quantity_delta=row.quantity_on_hand,
            reason=TransactionReason.ADJUSTMENT.value,
            note=note,
            state=TransactionState.PENDING.value,
        )
        qty_record.ordered += row.quantity_on_hand
        db.add(txn)
        db.flush()
        txn.commit(db)
        transaction_id = txn.id

    db.commit()

    return {
        "item": row.item,
        "status": "created",
        "row": row.row_number,
        "product_id": product.id,
        "air_filter_id": air_filter.id,
        "transaction_id": transaction_id,
        "quantity_on_hand": row.quantity_on_hand,
        "warehouse_id": warehouse_id,
        "height": row.height,
        "width": row.width,
        "depth": row.depth,
        "merv_rating": merv_rating,
    }


def main() -> int:
    args = parse_args()

    if not args.xlsx.exists():
        print(f"Excel file not found: {args.xlsx}", file=sys.stderr)
        return 1

    app = create_app()
    db = SessionLocal()

    counts = {
        "created": 0,
        "skipped": 0,
        "failed": 0,
        "dry_run": 0,
    }
    results: list[dict] = []
    processed = 0

    with app.app_context():
        from flask import g

        g.active_warehouse_id = args.warehouse_id

        for row in iter_item_list_addition_rows(args.xlsx, item_filter=args.item):
            if args.limit is not None and processed >= args.limit:
                break

            try:
                outcome = process_row(
                    db,
                    row,
                    warehouse_id=args.warehouse_id,
                    dry_run=args.dry_run,
                    note=args.note,
                    default_merv=args.default_merv,
                )
            except Exception as e:
                db.rollback()
                outcome = {
                    "item": row.item,
                    "status": "failed",
                    "reason": str(e),
                    "row": row.row_number,
                }

            results.append(outcome)
            status = outcome["status"]
            if status == "created":
                counts["created"] += 1
            elif status == "dry_run":
                counts["dry_run"] += 1
            elif status == "skipped":
                counts["skipped"] += 1
            elif status == "failed":
                counts["failed"] += 1

            processed += 1
            if processed % 20 == 0:
                print(f"Processed {processed} rows...")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "xlsx": str(args.xlsx),
        "warehouse_id": args.warehouse_id,
        "dry_run": args.dry_run,
        "counts": counts,
        "results": results,
    }

    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(counts, indent=2))
    print(f"Report written to {REPORT_PATH}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
