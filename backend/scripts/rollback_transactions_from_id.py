#!/usr/bin/env python3
"""
Bulk rollback committed transactions matching filters, starting from a minimum ID.

Designed to reverse duplicate Digital Count imports. Uses Transaction.rollback()
for committed transactions and Transaction.cancel() for any pending ones.

Usage:
  python scripts/rollback_transactions_from_id.py --dry-run --from-id 1949
  python scripts/rollback_transactions_from_id.py --from-id 1949
  python scripts/rollback_transactions_from_id.py --from-id 1949 --note "Digital Count import"
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
from database.models import Transaction, TransactionReason, TransactionState
from sqlalchemy import select

REPORT_PATH = Path(__file__).resolve().parent / "rollback_transactions_report.json"
DEFAULT_NOTE = "Digital Count import"
DEFAULT_REASON = TransactionReason.ADJUSTMENT.value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk rollback transactions from a minimum ID (inclusive)"
    )
    parser.add_argument(
        "--from-id",
        type=int,
        required=True,
        help="Minimum transaction ID to rollback (inclusive)",
    )
    parser.add_argument(
        "--warehouse-id",
        type=int,
        default=1,
        help="Warehouse scope (default: 1)",
    )
    parser.add_argument(
        "--note",
        type=str,
        default=DEFAULT_NOTE,
        help=f"Filter by note (default: {DEFAULT_NOTE!r})",
    )
    parser.add_argument(
        "--reason",
        type=str,
        default=DEFAULT_REASON,
        help=f"Filter by reason (default: {DEFAULT_REASON!r})",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def fetch_transactions(db, *, from_id: int, warehouse_id: int, note: str, reason: str) -> list[Transaction]:
    query = (
        select(Transaction)
        .where(
            Transaction.id >= from_id,
            Transaction.warehouse_id == warehouse_id,
            Transaction.note == note,
            Transaction.reason == reason,
            Transaction.state.in_(
                [TransactionState.COMMITTED.value, TransactionState.PENDING.value]
            ),
            Transaction.reason != TransactionReason.ROLLBACK.value,
        )
        .order_by(Transaction.id.desc())
    )
    return list(db.execute(query).scalars().all())


def process_transaction(db, txn: Transaction, *, dry_run: bool) -> dict:
    base = {
        "transaction_id": txn.id,
        "product_id": txn.product_id,
        "child_product_id": txn.child_product_id,
        "warehouse_id": txn.warehouse_id,
        "quantity_delta": txn.quantity_delta,
        "reason": txn.reason,
        "note": txn.note,
        "state": txn.state,
    }

    if txn.state == TransactionState.COMMITTED.value:
        if txn.reason == TransactionReason.ROLLBACK.value:
            return {**base, "status": "skipped", "skip_reason": "already a rollback transaction"}

        if dry_run:
            return {
                **base,
                "status": "dry_run",
                "action": "rollback",
                "reversal_delta": -txn.quantity_delta,
            }

        reversal = txn.rollback(db)
        db.commit()
        return {
            **base,
            "status": "rolled_back",
            "reversal_transaction_id": reversal.id,
            "reversal_delta": reversal.quantity_delta,
            "reversal_ledger_sequence": reversal.ledger_sequence,
        }

    if txn.state == TransactionState.PENDING.value:
        if dry_run:
            return {**base, "status": "dry_run", "action": "cancel"}

        txn.cancel()
        db.commit()
        return {**base, "status": "cancelled"}

    return {**base, "status": "skipped", "skip_reason": f"unsupported state: {txn.state}"}


def main() -> int:
    args = parse_args()

    app = create_app()
    db = SessionLocal()

    counts = {
        "rolled_back": 0,
        "cancelled": 0,
        "skipped": 0,
        "dry_run": 0,
        "failed": 0,
    }
    results: list[dict] = []

    with app.app_context():
        from flask import g

        g.active_warehouse_id = args.warehouse_id

        transactions = fetch_transactions(
            db,
            from_id=args.from_id,
            warehouse_id=args.warehouse_id,
            note=args.note,
            reason=args.reason,
        )

        print(
            f"Found {len(transactions)} transaction(s) matching filters "
            f"(id >= {args.from_id}, warehouse={args.warehouse_id}, "
            f"note={args.note!r}, reason={args.reason!r})"
        )

        if transactions:
            ids = [t.id for t in transactions]
            print(f"ID range: {min(ids)} – {max(ids)}")

        for i, txn in enumerate(transactions, start=1):
            try:
                outcome = process_transaction(db, txn, dry_run=args.dry_run)
            except ValueError as e:
                db.rollback()
                outcome = {
                    "transaction_id": txn.id,
                    "product_id": txn.product_id,
                    "quantity_delta": txn.quantity_delta,
                    "status": "failed",
                    "reason": str(e),
                }
                results.append(outcome)
                counts["failed"] += 1
                print(f"FAILED on transaction {txn.id}: {e}", file=sys.stderr)
                break
            except Exception as e:
                db.rollback()
                outcome = {
                    "transaction_id": txn.id,
                    "product_id": txn.product_id,
                    "quantity_delta": txn.quantity_delta,
                    "status": "failed",
                    "reason": str(e),
                }
                results.append(outcome)
                counts["failed"] += 1
                print(f"FAILED on transaction {txn.id}: {e}", file=sys.stderr)
                break

            results.append(outcome)
            status = outcome["status"]
            if status == "rolled_back":
                counts["rolled_back"] += 1
            elif status == "cancelled":
                counts["cancelled"] += 1
            elif status == "dry_run":
                counts["dry_run"] += 1
            elif status == "skipped":
                counts["skipped"] += 1

            if i % 50 == 0:
                print(f"Processed {i}/{len(transactions)}...")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "from_id": args.from_id,
        "warehouse_id": args.warehouse_id,
        "note": args.note,
        "reason": args.reason,
        "dry_run": args.dry_run,
        "total_matched": len(transactions),
        "counts": counts,
        "results": results,
    }

    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(counts, indent=2))
    print(f"Report written to {REPORT_PATH}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
