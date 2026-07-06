#!/usr/bin/env python3
"""
Bulk import orders from QuickBooks and apply tracker stages from tracking_packing_slips.xlsx.

Usage:
  python scripts/import_tracking_slips.py --dry-run --limit 20
  python scripts/import_tracking_slips.py --slip 11650
  python scripts/import_tracking_slips.py --update-existing

Tracker step completion rules (from tracking_packing_slips.xlsx):
  - Sales (MC/IS/RO), Warehouse/Delivery (GR), Service (OO): status must be "Delivered"
  - Logistics (SM): status must be "Finished"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend root is on path when run as scripts/import_tracking_slips.py
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import _startup  # noqa: F401
from app import create_app
from app.api.error_handling import DuplicateResourceError, ExternalServiceError
from app.services.qb_order_service import (
    create_order_from_qb_record,
    qb_doc_type_from_slip,
    query_qb_document,
)
from app.services.tracker_import_service import (
    apply_tracker_import,
    get_dept_cluster_completions,
)
from app.services.tracking_slips_xlsx import iter_tracking_slip_rows
from database import SessionLocal
from database.models import Order
from sqlalchemy import select
from sqlalchemy.orm import joinedload

DEFAULT_XLSX = (
    Path(__file__).resolve().parents[4]
    / "AFC Frontend"
    / "Frontend-AFC-Inventory"
    / "tracking_packing_slips.xlsx"
)

REPORT_PATH = Path(__file__).resolve().parent / "import_tracking_slips_report.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk import QB orders + tracker from Excel")
    parser.add_argument(
        "--xlsx",
        type=Path,
        default=DEFAULT_XLSX,
        help="Path to tracking_packing_slips.xlsx",
    )
    parser.add_argument("--warehouse-id", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-existing", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--slip", type=str, default=None)
    parser.add_argument("--qb-delay-ms", type=int, default=200)
    return parser.parse_args()


def process_row(
    db,
    row,
    *,
    warehouse_id: int,
    dry_run: bool,
    update_existing: bool,
) -> dict:
    slip = row.slip
    skip_reason = row.should_skip()
    if skip_reason:
        return {"slip": slip, "status": "skipped", "reason": skip_reason, "row": row.row_number}

    qb_doc_type = qb_doc_type_from_slip(slip)
    completions = get_dept_cluster_completions(row.order_type, row)
    mark_order_completed = row.is_order_completed
    completion_summary = {
        str(k): {"by": v.completed_by, "at": v.completed_at.isoformat()}
        for k, v in completions.items()
    }

    existing = db.execute(
        select(Order).where(Order.external_order_number == slip)
    ).scalar_one_or_none()

    if existing:
        should_update = update_existing or mark_order_completed or bool(completions)
        if not should_update:
            return {
                "slip": slip,
                "status": "skipped",
                "reason": "order already exists (use --update-existing)",
                "row": row.row_number,
                "order_id": existing.id,
            }
        if dry_run:
            return {
                "slip": slip,
                "status": "dry_run_update",
                "row": row.row_number,
                "order_id": existing.id,
                "qb_doc_type": qb_doc_type,
                "order_type": row.order_type,
                "stages": completion_summary,
                "is_backordered": row.is_backordered,
                "order_completed": mark_order_completed,
            }
        order = db.execute(
            select(Order).options(joinedload(Order.items)).where(Order.id == existing.id)
        ).unique().scalar_one()
        apply_tracker_import(
            db,
            order,
            completions,
            warehouse_id=warehouse_id,
            is_backordered=row.is_backordered,
            is_paid=row.is_paid,
            is_invoiced=row.is_invoiced,
            notes=row.notes,
            mark_order_completed=mark_order_completed,
            row=row,
        )
        return {
            "slip": slip,
            "status": "updated",
            "row": row.row_number,
            "order_id": existing.id,
            "stages": completion_summary,
            "order_completed": mark_order_completed,
        }

    if dry_run:
        return {
            "slip": slip,
            "status": "dry_run_create",
            "row": row.row_number,
            "qb_doc_type": qb_doc_type,
            "order_type": row.order_type,
            "stages": completion_summary,
            "is_backordered": row.is_backordered,
            "order_completed": mark_order_completed,
        }

    qb_query = query_qb_document(slip, qb_doc_type)
    if not qb_query.success:
        return {
            "slip": slip,
            "status": "skipped",
            "reason": f"QB not found: {qb_query.error_message}",
            "row": row.row_number,
            "qb_doc_type": qb_doc_type,
        }

    try:
        result = create_order_from_qb_record(
            db,
            reference_number=slip,
            qb_doc_type=qb_doc_type,
            order_type=row.order_type,
            warehouse_id=warehouse_id,
        )
    except DuplicateResourceError:
        return {
            "slip": slip,
            "status": "skipped",
            "reason": "duplicate order created concurrently",
            "row": row.row_number,
        }
    except (ExternalServiceError, ValueError) as e:
        return {
            "slip": slip,
            "status": "failed",
            "reason": str(e),
            "row": row.row_number,
        }

    apply_tracker_import(
        db,
        db.execute(
            select(Order).options(joinedload(Order.items)).where(Order.id == result.order.id)
        ).unique().scalar_one(),
        completions,
        warehouse_id=warehouse_id,
        is_backordered=row.is_backordered,
        is_paid=row.is_paid,
        is_invoiced=row.is_invoiced,
        notes=row.notes,
        mark_order_completed=mark_order_completed,
        row=row,
    )

    return {
        "slip": slip,
        "status": "created",
        "row": row.row_number,
        "order_id": result.order.id,
        "order_number": result.order.order_number,
        "stages": completion_summary,
        "order_completed": mark_order_completed,
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
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "dry_run": 0,
    }
    results: list[dict] = []
    processed = 0

    with app.app_context():
        from flask import g
        g.active_warehouse_id = args.warehouse_id

        for row in iter_tracking_slip_rows(args.xlsx, slip_filter=args.slip):
            if args.limit is not None and processed >= args.limit:
                break

            try:
                outcome = process_row(
                    db,
                    row,
                    warehouse_id=args.warehouse_id,
                    dry_run=args.dry_run,
                    update_existing=args.update_existing,
                )
            except Exception as e:
                db.rollback()
                outcome = {
                    "slip": row.slip,
                    "status": "failed",
                    "reason": str(e),
                    "row": row.row_number,
                }

            results.append(outcome)
            status = outcome["status"]
            if status == "created":
                counts["created"] += 1
            elif status == "updated":
                counts["updated"] += 1
            elif status in ("dry_run_create", "dry_run_update"):
                counts["dry_run"] += 1
            elif status == "skipped":
                counts["skipped"] += 1
            elif status == "failed":
                counts["failed"] += 1

            processed += 1
            if processed % 50 == 0:
                print(f"Processed {processed} rows...")

            if not args.dry_run and status in ("created", "updated"):
                time.sleep(args.qb_delay_ms / 1000.0)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "xlsx": str(args.xlsx),
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
