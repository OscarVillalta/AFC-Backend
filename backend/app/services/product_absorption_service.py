"""Absorb a duplicate Product into another Product as a ChildProduct."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.api.error_handling import InvalidInputError, ResourceNotFoundError
from database.models import (
    ChildProduct,
    OrderItem,
    Product,
    Quantity,
    Transaction,
)
from app.services.product_migration_service import _ensure_warehouse_quantities


@dataclass
class WarehouseQuantityMerge:
    warehouse_id: int
    on_hand: int
    reserved: int
    ordered: int


@dataclass
class AbsorptionResult:
    child_product_id: int
    parent_product_id: int
    archived_product_id: int
    transactions_repointed: int
    order_items_repointed: int
    children_reparented: int
    quantities_merged: list[WarehouseQuantityMerge] = field(default_factory=list)


def _load_product(db: Session, product_id: int) -> Product:
    product = db.execute(
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.category),
            selectinload(Product.quantities),
            selectinload(Product.child_products),
            selectinload(Product.air_filter),
            selectinload(Product.stock_item),
            selectinload(Product.media),
        )
    ).unique().scalar_one_or_none()
    if not product:
        raise ResourceNotFoundError("Product", product_id)
    return product


def _validate_absorption(source: Product, parent: Product) -> None:
    if source.id == parent.id:
        raise InvalidInputError("A product cannot be absorbed into itself.")

    if not source.is_active:
        raise InvalidInputError("Cannot absorb an archived source product.")

    if not parent.is_active:
        raise InvalidInputError("Cannot absorb into an archived parent product.")

    if source.category_id != parent.category_id:
        raise InvalidInputError(
            "Source and parent must share the same product category "
            "(Air Filters, Stock Items, or Media Items)."
        )

    if not source.reference_id:
        raise InvalidInputError("Source product has no catalog record to absorb.")

    if not (source.air_filter or source.stock_item or source.media):
        raise InvalidInputError("Source product has no resolvable catalog row.")

    for child in parent.child_products:
        if child.reference_id == source.reference_id:
            raise InvalidInputError(
                "Parent product already has a child product with the same catalog reference."
            )


def _merge_quantities(
    db: Session,
    *,
    source: Product,
    parent: Product,
) -> list[WarehouseQuantityMerge]:
    _ensure_warehouse_quantities(db, parent)
    db.flush()

    source_by_warehouse = {q.warehouse_id: q for q in source.quantities}
    parent_by_warehouse = {q.warehouse_id: q for q in parent.quantities}
    warehouse_ids = set(source_by_warehouse) | set(parent_by_warehouse)

    merges: list[WarehouseQuantityMerge] = []

    for warehouse_id in sorted(warehouse_ids):
        source_qty = source_by_warehouse.get(warehouse_id)
        parent_qty = parent_by_warehouse.get(warehouse_id)

        if not parent_qty:
            parent_qty = Quantity(
                product_id=parent.id,
                warehouse_id=warehouse_id,
                on_hand=0,
                reserved=0,
                ordered=0,
                location=0,
            )
            db.add(parent_qty)
            db.flush()
            parent_by_warehouse[warehouse_id] = parent_qty

        if not source_qty:
            continue

        parent_qty = db.execute(
            select(Quantity)
            .where(
                Quantity.product_id == parent.id,
                Quantity.warehouse_id == warehouse_id,
            )
            .with_for_update()
        ).scalar_one()

        delta = WarehouseQuantityMerge(
            warehouse_id=warehouse_id,
            on_hand=source_qty.on_hand,
            reserved=source_qty.reserved,
            ordered=source_qty.ordered,
        )
        parent_qty.on_hand += source_qty.on_hand
        parent_qty.reserved += source_qty.reserved
        parent_qty.ordered += source_qty.ordered
        merges.append(delta)
        db.delete(source_qty)

    db.flush()
    return merges


def absorb_product_into_parent(
    db: Session,
    *,
    source_product_id: int,
    parent_product_id: int,
) -> AbsorptionResult:
    source = _load_product(db, source_product_id)
    parent = _load_product(db, parent_product_id)
    _validate_absorption(source, parent)

    child = ChildProduct(
        category_id=source.category_id,
        reference_id=source.reference_id,
        parent_product_id=parent.id,
        is_active=True,
    )
    db.add(child)
    db.flush()

    quantities_merged = _merge_quantities(db, source=source, parent=parent)

    txn_result = db.execute(
        update(Transaction)
        .where(Transaction.product_id == source.id)
        .values(product_id=None, child_product_id=child.id)
    )
    transactions_repointed = txn_result.rowcount or 0

    oi_result = db.execute(
        update(OrderItem)
        .where(OrderItem.product_id == source.id)
        .values(product_id=None, child_product_id=child.id)
    )
    order_items_repointed = oi_result.rowcount or 0

    reparent_result = db.execute(
        update(ChildProduct)
        .where(
            ChildProduct.parent_product_id == source.id,
            ChildProduct.id != child.id,
        )
        .values(parent_product_id=parent.id)
    )
    children_reparented = reparent_result.rowcount or 0

    source.is_active = False
    db.flush()

    return AbsorptionResult(
        child_product_id=child.id,
        parent_product_id=parent.id,
        archived_product_id=source.id,
        transactions_repointed=transactions_repointed,
        order_items_repointed=order_items_repointed,
        children_reparented=children_reparented,
        quantities_merged=quantities_merged,
    )
