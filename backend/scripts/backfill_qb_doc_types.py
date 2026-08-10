#!/usr/bin/env python3
"""
Backfill orders.qb_doc_type and report duplicate (external_order_number, qb_doc_type) groups.

Inference rules (priority order):
  1. incoming -> purchase_order
  2. numeric external_order_number > 2000 -> invoice
  3. else -> sales_order

Rows with NULL/blank external_order_number or type=void are left NULL.

Usage:
  python scripts/backfill_qb_doc_types.py --dry-run
  python scripts/backfill_qb_doc_types.py
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
from app.services.qb_order_service import infer_qb_doc_type_for_order
from database import SessionLocal
from database.models import Order
from sqlalchemy import select, text

REPORT_PATH = Path(__file__).resolve().parent / "backfill_qb_doc_types_report.json"

DUPLICATE_GROUPS_SQL = text("""
    SELECT
        external_order_number,
        qb_doc_type,
        COUNT(*) AS row_count,
        ARRAY_AGG(id ORDER BY id) AS order_ids
    FROM orders
    WHERE external_order_number IS NOT NULL
      AND BTRIM(external_order_number) <> ''
      AND qb_doc_type IS NOT NULL
    GROUP BY external_order_number, qb_doc_type
    HAVING COUNT(*) > 1
    ORDER BY external_order_number, qb_doc_type
""")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill orders.qb_doc_type and audit duplicates")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = create_app()
    db = SessionLocal()

    updated = 0
    counts: dict[str, int] = {}
    changes: list[dict] = []

    with app.app_context():
        orders = db.scalars(
            select(Order)
            .where(Order.qb_doc_type.is_(None))
            .order_by(Order.id)
        ).all()

        for order in orders:
            inferred = infer_qb_doc_type_for_order(order.type, order.external_order_number)
            if inferred is None:
                continue
            changes.append(
                {
                    "order_id": order.id,
                    "external_order_number": order.external_order_number,
                    "type": order.type,
                    "qb_doc_type": inferred,
                }
            )
            counts[inferred] = counts.get(inferred, 0) + 1
            if not args.dry_run:
                order.qb_doc_type = inferred
                updated += 1

        if not args.dry_run and updated:
            db.commit()

        duplicate_groups = [
            dict(row)
            for row in db.execute(DUPLICATE_GROUPS_SQL).mappings().all()
        ]

        type_totals = db.execute(
            text("""
                SELECT qb_doc_type, COUNT(*) AS cnt
                FROM orders
                WHERE qb_doc_type IS NOT NULL
                GROUP BY qb_doc_type
                ORDER BY qb_doc_type
            """)
        ).mappings().all()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "would_update" if args.dry_run else "updated": len(changes),
        "inferred_counts": counts,
        "current_totals_by_type": {row["qb_doc_type"]: row["cnt"] for row in type_totals},
        "duplicate_groups": duplicate_groups,
        "sample_changes": changes[:50],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {REPORT_PATH}")
    if duplicate_groups:
        print(f"\nWARNING: {len(duplicate_groups)} duplicate (external_order_number, qb_doc_type) group(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
