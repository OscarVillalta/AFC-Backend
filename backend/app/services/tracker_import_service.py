"""Bulk tracker stage import from Excel tracking spreadsheet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.api.error_handling import safe_commit
from app.services.tracker_steps import (
    first_incomplete_index,
    stage_indices_for_department,
    steps_for_order_type,
)
from database.models import (
    Department,
    Order,
    OrderItem,
    OrderItemType,
    OrderStatus,
    OrderTracker,
    OrderTrackerStage,
    OrderType,
)

if TYPE_CHECKING:
    from app.services.tracking_slips_xlsx import TrackingSlipRow


@dataclass
class StageCompletion:
    completed_at: datetime
    completed_by: str


SALES_COLUMNS = ("MC", "IS", "RO")
LOGISTICS_COLUMNS = ("SM",)
DELIVERY_COLUMNS = ("GR",)  # Warehouse / Delivery column in spreadsheet
SERVICE_COLUMNS = ("OO",)

DELIVERED_STATUS = "Delivered"
FINISHED_STATUS = "Finished"

# department -> (required column status, excel columns)
DEPARTMENT_COLUMN_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    Department.SALES.value: (DELIVERED_STATUS, SALES_COLUMNS),
    Department.LOGISTICS.value: (FINISHED_STATUS, LOGISTICS_COLUMNS),
    Department.DELIVERY_DEPT.value: (DELIVERED_STATUS, DELIVERY_COLUMNS),
    Department.SERVICE.value: (DELIVERED_STATUS, SERVICE_COLUMNS),
}


def _column_status_matches(status_val, required_status: str) -> bool:
    if status_val is None:
        return False
    return str(status_val).strip().lower() == required_status.strip().lower()


def _latest_column_completion(
    row: TrackingSlipRow,
    columns: tuple[str, ...],
    *,
    required_status: str,
) -> StageCompletion | None:
    """Return latest date among columns with matching status and a date filled."""
    best_date: datetime | None = None
    best_col: str | None = None

    for col in columns:
        status_val = getattr(row, col, None)
        date_val = getattr(row, f"date{col}", None)

        if (
            _column_status_matches(status_val, required_status)
            and date_val is not None
        ):
            if best_date is None or date_val > best_date:
                best_date = date_val
                best_col = col

    if best_date is None or best_col is None:
        return None

    return StageCompletion(completed_at=best_date, completed_by=best_col)


def get_dept_cluster_completions(
    order_type: str,
    row: TrackingSlipRow,
) -> dict[int, StageCompletion]:
    """
    Map Excel user columns to tracker stage indices via department clusters.
    Sales, Warehouse (GR/Delivery), and Service require column status "Delivered".
    Logistics requires column status "Finished".
    """
    completions: dict[int, StageCompletion] = {}
    is_installation = (order_type or "").lower() == OrderType.INSTALLATION.value

    for department, (required_status, columns) in DEPARTMENT_COLUMN_GROUPS.items():
        if department == Department.SERVICE.value and not is_installation:
            continue

        cluster = _latest_column_completion(
            row, columns, required_status=required_status
        )
        if cluster is None:
            continue

        for stage_index in stage_indices_for_department(order_type, department):
            completions[stage_index] = cluster

    return completions


def _ensure_tracker(db, order: Order, warehouse_id: int) -> OrderTracker:
    tracker = db.execute(
        select(OrderTracker).where(OrderTracker.order_id == order.id)
    ).scalar_one_or_none()

    if tracker is None:
        steps = steps_for_order_type(order.type)
        initial_dept = steps[0] if steps else Department.SALES.value
        tracker = OrderTracker(
            order_id=order.id,
            warehouse_id=warehouse_id,
            current_department=initial_dept,
            step_index=0,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(tracker)
        db.flush()

    return tracker


def resolve_order_completed_at(
    completions: dict[int, StageCompletion],
    row: TrackingSlipRow | None,
) -> datetime:
    """Pick the best historical completion timestamp for the order."""
    candidates: list[datetime] = [c.completed_at for c in completions.values()]
    if row is not None:
        for attr in ("datePA", "date_created", "dateRO", "dateSM", "dateGR", "dateOO", "dateMC", "dateIS"):
            dt = getattr(row, attr, None)
            if dt is not None:
                candidates.append(dt)
    if candidates:
        return max(candidates)
    return datetime.now(timezone.utc)


def apply_completed_order_state(
    db,
    order: Order,
    *,
    completed_at: datetime | None = None,
) -> None:
    """
    Mark an imported order as Completed with no inventory impact.
    Sets no_stock_deduction on all product line items and fulfills quantities.
    """
    if order.type == OrderType.VOID.value:
        return

    completed_at = completed_at or datetime.now(timezone.utc)
    is_incoming = order.type == OrderType.INCOMING.value

    items = order.items
    if not items:
        items = db.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id)
        ).all()

    for item in items:
        if item.type in (
            OrderItemType.UNIT_SEPARATOR.value,
            OrderItemType.SECTION_SEPARATOR.value,
        ):
            continue
        if not is_incoming:
            item.no_stock_deduction = True
        item.quantity_fulfilled = item.quantity_ordered

    order.status = OrderStatus.COMPLETED.value
    order.completed_at = completed_at


def apply_tracker_import(
    db,
    order: Order,
    completions: dict[int, StageCompletion],
    *,
    warehouse_id: int,
    is_backordered: bool = False,
    is_paid: bool = False,
    is_invoiced: bool = False,
    notes: str | None = None,
    mark_order_completed: bool = False,
    order_completed_at: datetime | None = None,
    row: TrackingSlipRow | None = None,
) -> None:
    """Upsert tracker stages with historical dates and update order flags."""
    steps = steps_for_order_type(order.type)
    total_steps = len(steps)

    is_incoming = order.type == OrderType.INCOMING.value
    tracker = _ensure_tracker(db, order, warehouse_id)

    existing_stages = list(
        db.execute(
            select(OrderTrackerStage).where(OrderTrackerStage.order_id == order.id)
        ).scalars().all()
    )
    stage_by_index = {s.stage_index: s for s in existing_stages}

    for stage_index, completion in completions.items():
        if stage_index < 0 or stage_index >= total_steps:
            continue

        stage = stage_by_index.get(stage_index)
        if stage is None:
            stage = OrderTrackerStage(
                order_id=order.id,
                stage_index=stage_index,
                is_completed=True,
                completed_by=completion.completed_by,
                completed_at=completion.completed_at,
            )
            db.add(stage)
            stage_by_index[stage_index] = stage
            existing_stages.append(stage)
        else:
            stage.is_completed = True
            stage.completed_by = completion.completed_by
            stage.completed_at = completion.completed_at

    new_first_incomplete = first_incomplete_index(existing_stages, total_steps)
    if new_first_incomplete >= 0:
        tracker.current_department = steps[new_first_incomplete]
        tracker.step_index = new_first_incomplete
    else:
        tracker.current_department = steps[-1]
        tracker.step_index = total_steps - 1

    if not is_incoming:
        tracker.is_backordered = is_backordered
    tracker.updated_at = datetime.now(timezone.utc)

    order.is_paid = is_paid
    order.is_invoiced = is_invoiced

    if notes and notes.strip():
        existing = (order.description or "").strip()
        note_text = notes.strip()
        if existing:
            if note_text not in existing:
                order.description = f"{existing}\n{note_text}"
        else:
            order.description = note_text

    if mark_order_completed:
        apply_completed_order_state(
            db,
            order,
            completed_at=order_completed_at or resolve_order_completed_at(completions, row),
        )

    safe_commit(db, "applying tracker import")
