#!/usr/bin/env python3
"""
Recompute Quantity.reserved from pending outgoing transactions.

Reserved should equal the sum of |quantity_delta| for pending transactions
with quantity_delta < 0, scoped to product + warehouse (parent product for
child-product lines).

Usage:
  python scripts/reconcile_reserved.py --dry-run
  python scripts/reconcile_reserved.py
  python scripts/reconcile_reserved.py --product-ids 27600,27662
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
from database import SessionLocal
from database.models import Quantity
from sqlalchemy import select, text

REPORT_PATH = Path(__file__).resolve().parent / "reconcile_reserved_report.json"

PENDING_RESERVED_SQL = text("""
    SELECT
        COALESCE(t.product_id, cp.parent_product_id) AS product_id,
        t.warehouse_id,
        COALESCE(SUM(ABS(t.quantity_delta)), 0)::int AS expected_reserved
    FROM transactions t
    LEFT JOIN child_products cp ON cp.id = t.child_product_id
    WHERE t.state = 'pending'
      AND t.quantity_delta < 0
      AND COALESCE(t.product_id, cp.parent_product_id) IS NOT NULL
    GROUP BY 1, 2
""")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute quantities.reserved from pending outgoing transactions"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--product-ids",
        type=str,
        default=None,
        help="Comma-separated product IDs to limit scope (default: all)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    product_filter: set[int] | None = None
    if args.product_ids:
        product_filter = {int(x.strip()) for x in args.product_ids.split(",") if x.strip()}

    app = create_app()
    db = SessionLocal()

    changes: list[dict] = []
    updated = 0

    with app.app_context():
        expected_rows = db.execute(PENDING_RESERVED_SQL).mappings().all()
        expected_map = {
            (row["product_id"], row["warehouse_id"]): row["expected_reserved"]
            for row in expected_rows
        }

        qty_query = select(Quantity)
        if product_filter:
            qty_query = qty_query.where(Quantity.product_id.in_(product_filter))

        quantities = db.scalars(qty_query).all()

        for qty in quantities:
            key = (qty.product_id, qty.warehouse_id)
            expected = expected_map.get(key, 0)
            if qty.reserved == expected:
                continue

            changes.append({
                "product_id": qty.product_id,
                "warehouse_id": qty.warehouse_id,
                "reserved_before": qty.reserved,
                "reserved_after": expected,
            })

            if not args.dry_run:
                qty.reserved = expected
                updated += 1

        if not args.dry_run and updated:
            db.commit()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "rows_changed": len(changes),
        "changes": changes,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({
        "dry_run": args.dry_run,
        "rows_changed": len(changes),
        "report": str(REPORT_PATH),
    }, indent=2))

    if changes:
        print("\nSample changes:")
        for row in changes[:20]:
            print(row)
        if len(changes) > 20:
            print(f"... and {len(changes) - 20} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
