#!/usr/bin/env python3
"""
Offset committed order transactions whose stock effect is already reflected in
a physical count.

For every committed transaction that matches the filters, a compensating
"adjustment" transaction with the opposite quantity is created (already
committed) and applied to on_hand. Orders, order lines, fulfillment and
order status are NOT changed.

Default filters (the 2026-10-05 Eisenhower / ZLP fix):
  - order customer name contains "eisenhower"
  - product name (part number / stock item name) contains "ZLP"
  - transaction last_updated_at falls on 2026-10-05 in America/Los_Angeles

Safe to re-run: transactions that already have an offset are skipped.

Usage:
  python scripts/offset_count_transactions.py --dry-run
  python scripts/offset_count_transactions.py
  python scripts/offset_count_transactions.py --customer eisenhower --product-name ZLP --date 2026-10-05
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import _startup  # noqa: F401
from app import create_app
from database import SessionLocal
from database.models import Transaction, TransactionReason, TransactionState
from sqlalchemy import select, text

REPORT_PATH = Path(__file__).resolve().parent / "offset_count_transactions_report.json"
OFFSET_NOTE_PREFIX = "Count offset of transaction #"

MATCHING_TXNS_SQL = text("""
    SELECT t.id,
           t.quantity_delta,
           t.last_updated_at,
           o.order_number,
           c.name AS customer_name,
           COALESCE(af.part_number, si.name, m.part_number,
                    caf.part_number, csi.name, cm.part_number) AS product_name
    FROM transactions t
    JOIN orders o ON o.id = t.order_id
    JOIN customers c ON c.id = o.customer_id
    LEFT JOIN products p ON p.id = t.product_id
    LEFT JOIN air_filters af ON p.category_id = 1 AND af.id = p.reference_id
    LEFT JOIN stock_items si ON p.category_id = 3 AND si.id = p.reference_id
    LEFT JOIN media m ON p.category_id = 4 AND m.id = p.reference_id
    LEFT JOIN child_products cp ON cp.id = t.child_product_id
    LEFT JOIN air_filters caf ON cp.category_id = 1 AND caf.id = cp.reference_id
    LEFT JOIN stock_items csi ON cp.category_id = 3 AND csi.id = cp.reference_id
    LEFT JOIN media cm ON cp.category_id = 4 AND cm.id = cp.reference_id
    WHERE t.state = :committed
      AND t.reason <> :rollback
      AND c.name ILIKE :customer
      AND COALESCE(af.part_number, si.name, m.part_number,
                   caf.part_number, csi.name, cm.part_number) ILIKE :product_name
      AND t.last_updated_at >= :start_utc
      AND t.last_updated_at < :end_utc
    ORDER BY t.last_updated_at, t.id
""")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offset committed order transactions already reflected in a count")
    parser.add_argument("--customer", default="eisenhower", help="Customer name contains (case-insensitive)")
    parser.add_argument("--product-name", default="ZLP", help="Product name contains (case-insensitive)")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 10, 5), help="Local date, YYYY-MM-DD")
    parser.add_argument("--tz", default="America/Los_Angeles", help="Timezone the date is in")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be offset without writing")
    return parser.parse_args()


def utc_bounds(local_day: date, tz_name: str) -> tuple[datetime, datetime]:
    """Naive UTC [start, end) for a local calendar day (timestamps are stored as naive UTC)."""
    tz = ZoneInfo(tz_name)
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def already_offset_ids(db, txn_ids: list[int]) -> set[int]:
    if not txn_ids:
        return set()
    notes = db.scalars(
        select(Transaction.note).where(
            Transaction.reason == TransactionReason.ADJUSTMENT.value,
            Transaction.note.like(f"{OFFSET_NOTE_PREFIX}%"),
        )
    ).all()
    offset: set[int] = set()
    for note in notes:
        ref = note[len(OFFSET_NOTE_PREFIX):].split(" ", 1)[0]
        if ref.isdigit():
            offset.add(int(ref))
    return offset & set(txn_ids)


def main() -> int:
    args = parse_args()
    start_utc, end_utc = utc_bounds(args.date, args.tz)

    app = create_app()
    db = SessionLocal()

    offsets: list[dict] = []
    skipped_already_offset: list[int] = []
    totals_by_product: dict[str, int] = defaultdict(int)

    try:
        with app.app_context():
            matches = db.execute(MATCHING_TXNS_SQL, {
                "committed": TransactionState.COMMITTED.value,
                "rollback": TransactionReason.ROLLBACK.value,
                "customer": f"%{args.customer}%",
                "product_name": f"%{args.product_name}%",
                "start_utc": start_utc,
                "end_utc": end_utc,
            }).mappings().all()

            done = already_offset_ids(db, [m["id"] for m in matches])

            for match in matches:
                if match["id"] in done:
                    skipped_already_offset.append(match["id"])
                    continue

                original = db.get(Transaction, match["id"])
                qty_record = original._get_quantity_record()
                if qty_record is None:
                    raise RuntimeError(f"Quantity record missing for transaction #{original.id}")

                offset_delta = -original.quantity_delta
                on_hand_before = qty_record.on_hand
                offsets.append({
                    "original_txn_id": original.id,
                    "order_number": match["order_number"],
                    "product_name": match["product_name"],
                    "warehouse_id": original.warehouse_id,
                    "original_delta": original.quantity_delta,
                    "offset_delta": offset_delta,
                    "on_hand_before": on_hand_before,
                    "on_hand_after": on_hand_before + offset_delta,
                })
                totals_by_product[match["product_name"]] += offset_delta

                if args.dry_run:
                    qty_record.on_hand += offset_delta
                    continue

                offset_txn = Transaction(
                    product_id=original.product_id,
                    child_product_id=original.child_product_id,
                    warehouse_id=original.warehouse_id,
                    quantity_delta=offset_delta,
                    reason=TransactionReason.ADJUSTMENT.value,
                    state=TransactionState.COMMITTED.value,
                    note=(
                        f"{OFFSET_NOTE_PREFIX}{original.id} "
                        f"(order {match['order_number']}, already reflected in physical count)"
                    ),
                )
                offset_txn.ledger_sequence = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
                offset_txn.last_updated_at = datetime.now(timezone.utc)
                qty_record.on_hand += offset_delta
                db.add(offset_txn)

            if args.dry_run:
                db.rollback()
            elif offsets:
                db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "filters": {
            "customer_contains": args.customer,
            "product_name_contains": args.product_name,
            "local_date": args.date.isoformat(),
            "timezone": args.tz,
            "utc_window": [start_utc.isoformat(), end_utc.isoformat()],
        },
        "matching_committed_transactions": len(offsets) + len(skipped_already_offset),
        "would_offset" if args.dry_run else "offset": len(offsets),
        "skipped_already_offset": len(skipped_already_offset),
        "on_hand_restored_total": sum(totals_by_product.values()),
        "on_hand_restored_by_product": dict(sorted(totals_by_product.items())),
    }
    REPORT_PATH.write_text(
        json.dumps({**summary, "skipped_txn_ids": skipped_already_offset, "offsets": offsets}, indent=2, default=str),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, default=str))
    print(f"\nFull report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
