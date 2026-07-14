#!/usr/bin/env python3
"""
Import physical counts from Digital Count.xlsm into warehouse 1 inventory.

For each row where Physical Count > 0, creates a committed adjustment transaction
for the product matching the Item column (part number / stock item name).

Usage:
  python scripts/import_digital_count.py --dry-run
  python scripts/import_digital_count.py --dry-run --limit 20
  python scripts/import_digital_count.py --item 1010-2ply
  python scripts/import_digital_count.py
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
from app.services.digital_count_xlsx import iter_digital_count_rows
from app.services.qb_order_service import find_product_by_name
from database import SessionLocal
from database.models import Quantity, Transaction, TransactionReason, TransactionState
from sqlalchemy import select

DEFAULT_XLSX = Path(__file__).resolve().parents[2] / "Digital Count.xlsm"
REPORT_PATH = Path(__file__).resolve().parent / "import_digital_count_report.json"
DEFAULT_NOTE = "Digital Count import"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import physical counts from Digital Count.xlsm (warehouse 1)"
    )
    parser.add_argument(
        "--xlsx",
        type=Path,
        default=DEFAULT_XLSX,
        help="Path to Digital Count.xlsm",
    )
    parser.add_argument(
        "--warehouse-id",
        type=int,
        default=1,
        help="Warehouse to apply transactions to (default: 1)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--item", type=str, default=None, help="Import a single item only")
    parser.add_argument("--note", type=str, default=DEFAULT_NOTE)
    return parser.parse_args()


def process_row(
    db,
    row,
    *,
    warehouse_id: int,
    dry_run: bool,
    note: str,
) -> dict:
    skip_reason = row.should_skip()
    if skip_reason:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": skip_reason,
            "row": row.row_number,
            "physical_count": row.physical_count,
        }

    product = find_product_by_name(db, row.item)
    if not product:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": "product not found",
            "row": row.row_number,
            "physical_count": row.physical_count,
        }

    qty_record = db.execute(
        select(Quantity).where(
            (Quantity.product_id == product.id) & (Quantity.warehouse_id == warehouse_id)
        )
    ).scalar_one_or_none()

    if not qty_record:
        return {
            "item": row.item,
            "status": "skipped",
            "reason": f"no quantity record for warehouse {warehouse_id}",
            "row": row.row_number,
            "product_id": product.id,
            "physical_count": row.physical_count,
        }

    if dry_run:
        return {
            "item": row.item,
            "status": "dry_run",
            "row": row.row_number,
            "product_id": product.id,
            "warehouse_id": warehouse_id,
            "quantity_delta": row.physical_count,
            "quantity_on_hand_before": qty_record.on_hand,
            "physical_count": row.physical_count,
        }

    txn = Transaction(
        product_id=product.id,
        warehouse_id=warehouse_id,
        quantity_delta=row.physical_count,
        reason=TransactionReason.ADJUSTMENT.value,
        note=note,
        state=TransactionState.PENDING.value,
    )

    qty_record.ordered += row.physical_count
    db.add(txn)
    db.flush()
    txn.commit(db)
    db.commit()

    return {
        "item": row.item,
        "status": "created",
        "row": row.row_number,
        "product_id": product.id,
        "transaction_id": txn.id,
        "warehouse_id": warehouse_id,
        "quantity_delta": row.physical_count,
        "quantity_on_hand_after": qty_record.on_hand,
        "ledger_sequence": txn.ledger_sequence,
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

        for row in iter_digital_count_rows(args.xlsx, item_filter=args.item):
            if args.limit is not None and processed >= args.limit:
                break

            try:
                outcome = process_row(
                    db,
                    row,
                    warehouse_id=args.warehouse_id,
                    dry_run=args.dry_run,
                    note=args.note,
                )
            except Exception as e:
                db.rollback()
                outcome = {
                    "item": row.item,
                    "status": "failed",
                    "reason": str(e),
                    "row": row.row_number,
                    "physical_count": row.physical_count,
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
            if processed % 50 == 0:
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
