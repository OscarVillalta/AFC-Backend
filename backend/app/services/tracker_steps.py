"""Shared tracker step definitions — mirrors tracker.py and frontend trackerSteps.ts."""

from __future__ import annotations

from database.models import Department, OrderType

INSTALLATION_STEPS = [
    Department.SALES.value,
    Department.LOGISTICS.value,
    Department.DELIVERY_DEPT.value,
    Department.SERVICE.value,
    Department.SALES.value,
    Department.LOGISTICS.value,
]

WILL_CALL_STEPS = [
    Department.SALES.value,
    Department.LOGISTICS.value,
    Department.DELIVERY_DEPT.value,
    Department.LOGISTICS.value,
]

PURCHASE_ORDER_STEPS = [
    Department.LOGISTICS.value,
    Department.DELIVERY_DEPT.value,
    Department.LOGISTICS.value,
]


def steps_for_order_type(order_type: str) -> list[str]:
    t = (order_type or "").lower()
    if t == OrderType.INSTALLATION.value:
        return INSTALLATION_STEPS
    if t == OrderType.INCOMING.value:
        return PURCHASE_ORDER_STEPS
    return WILL_CALL_STEPS


def stage_indices_for_department(order_type: str, department: str) -> list[int]:
    """Return all stage indices whose department matches."""
    steps = steps_for_order_type(order_type)
    return [i for i, dept in enumerate(steps) if dept == department]


def first_incomplete_index(stages: list, total_steps: int) -> int:
    stage_map = {s.stage_index: s for s in stages}
    for i in range(total_steps):
        if not stage_map.get(i) or not stage_map[i].is_completed:
            return i
    return -1
