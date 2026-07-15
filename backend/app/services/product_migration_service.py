"""Migrate a product (and child products) between catalog sub-tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.error_handling import DuplicateResourceError, InvalidInputError, ResourceNotFoundError
from database.models import (
    AirFilter,
    AirFilterCategory,
    ChildProduct,
    Media,
    MediaCategory,
    Product,
    ProductCategory,
    Quantity,
    StockItem,
    StockItemCategory,
    Transaction,
    TransactionState,
    Warehouse,
)

TargetType = Literal["air_filters", "stock_items", "media"]

TARGET_TO_PRODUCT_CATEGORY = {
    "air_filters": "Air Filters",
    "stock_items": "Stock Items",
    "media": "Media Items",
}

SOURCE_TYPE_FROM_CATEGORY = {
    "Air Filters": "air_filters",
    "Stock Items": "stock_items",
    "Media Items": "media",
}


@dataclass
class CatalogSnapshot:
    source_type: TargetType
    identifier: str
    description: str | None
    supplier_id: int
    merv_rating: int = 0
    height: int = 0
    width: int = 0
    depth: int = 0
    length: float | None = None
    media_width: float | None = None
    unit_of_measure: str | None = None


def _snapshot_from_air_filter(row: AirFilter) -> CatalogSnapshot:
    return CatalogSnapshot(
        source_type="air_filters",
        identifier=row.part_number,
        description=row.description,
        supplier_id=row.supplier_id,
        merv_rating=row.merv_rating,
        height=row.height,
        width=row.width,
        depth=row.depth,
    )


def _snapshot_from_stock_item(row: StockItem) -> CatalogSnapshot:
    return CatalogSnapshot(
        source_type="stock_items",
        identifier=row.name,
        description=row.description,
        supplier_id=row.supplier_id,
    )


def _snapshot_from_media(row: Media) -> CatalogSnapshot:
    return CatalogSnapshot(
        source_type="media",
        identifier=row.part_number,
        description=row.description,
        supplier_id=row.supplier_id,
        length=row.length,
        media_width=row.width,
        unit_of_measure=row.unit_of_measure,
        height=int(row.width) if row.width is not None else 0,
        width=int(row.length) if row.length is not None else 0,
    )


def _load_catalog_snapshot(product: Product) -> CatalogSnapshot:
    if product.air_filter:
        return _snapshot_from_air_filter(product.air_filter)
    if product.stock_item:
        return _snapshot_from_stock_item(product.stock_item)
    if product.media:
        return _snapshot_from_media(product.media)
    raise InvalidInputError("Product has no catalog record to migrate.")


def _load_child_snapshot(child: ChildProduct) -> CatalogSnapshot:
    if child.air_filter:
        return _snapshot_from_air_filter(child.air_filter)
    if child.stock_item:
        return _snapshot_from_stock_item(child.stock_item)
    if child.media:
        return _snapshot_from_media(child.media)
    raise InvalidInputError(f"Child product {child.id} has no catalog record to migrate.")


def _merge_overrides(snapshot: CatalogSnapshot, overrides: dict[str, Any] | None) -> CatalogSnapshot:
    if not overrides:
        return snapshot
    data = snapshot.__dict__.copy()
    for key, value in overrides.items():
        if value is not None and key in data:
            data[key] = value
    if overrides.get("part_number"):
        data["identifier"] = overrides["part_number"]
    if overrides.get("name"):
        data["identifier"] = overrides["name"]
    return CatalogSnapshot(**data)


def _validate_target_category(db: Session, target_type: TargetType, category_id: int) -> None:
    model_map = {
        "air_filters": AirFilterCategory,
        "stock_items": StockItemCategory,
        "media": MediaCategory,
    }
    model = model_map[target_type]
    if not db.get(model, category_id):
        raise InvalidInputError(f"Invalid target category ID for {target_type}.")


def _identifier_exists(db: Session, target_type: TargetType, identifier: str, *, exclude_id: int | None = None) -> bool:
    if target_type == "stock_items":
        query = select(StockItem.id).where(StockItem.name == identifier)
        if exclude_id is not None:
            query = query.where(StockItem.id != exclude_id)
    elif target_type == "air_filters":
        query = select(AirFilter.id).where(AirFilter.part_number == identifier)
        if exclude_id is not None:
            query = query.where(AirFilter.id != exclude_id)
    else:
        query = select(Media.id).where(Media.part_number == identifier)
        if exclude_id is not None:
            query = query.where(Media.id != exclude_id)
    return db.scalar(query.limit(1)) is not None


def _assert_identifier_available(db: Session, target_type: TargetType, identifier: str) -> None:
    if _identifier_exists(db, target_type, identifier):
        label = "name" if target_type == "stock_items" else "part_number"
        raise DuplicateResourceError("Catalog item", label, identifier)


def _has_pending_transactions(db: Session, product_id: int, child_ids: list[int]) -> bool:
    query = (
        select(Transaction.id)
        .where(Transaction.state == TransactionState.PENDING.value)
        .where(Transaction.product_id == product_id)
    )
    if child_ids:
        query = select(Transaction.id).where(
            Transaction.state == TransactionState.PENDING.value,
            (Transaction.product_id == product_id) | (Transaction.child_product_id.in_(child_ids)),
        )
    return db.scalar(query.limit(1)) is not None


def _create_target_row(
    db: Session,
    *,
    target_type: TargetType,
    snapshot: CatalogSnapshot,
    target_category_id: int,
):
    if target_type == "air_filters":
        row = AirFilter(
            part_number=snapshot.identifier,
            description=snapshot.description,
            supplier_id=snapshot.supplier_id,
            category_id=target_category_id,
            merv_rating=snapshot.merv_rating,
            height=snapshot.height,
            width=snapshot.width,
            depth=snapshot.depth,
        )
    elif target_type == "stock_items":
        row = StockItem(
            name=snapshot.identifier,
            description=snapshot.description,
            supplier_id=snapshot.supplier_id,
            category_id=target_category_id,
        )
    else:
        length = snapshot.length
        width = snapshot.media_width
        if length is None and snapshot.height:
            length = float(snapshot.height)
        if width is None and snapshot.width:
            width = float(snapshot.width)
        row = Media(
            part_number=snapshot.identifier,
            description=snapshot.description,
            supplier_id=snapshot.supplier_id,
            category_id=target_category_id,
            length=length,
            width=width,
            unit_of_measure=snapshot.unit_of_measure,
        )
    db.add(row)
    db.flush()
    return row


def _delete_source_row(db: Session, source_type: TargetType, reference_id: int) -> None:
    if source_type == "air_filters":
        row = db.get(AirFilter, reference_id)
    elif source_type == "stock_items":
        row = db.get(StockItem, reference_id)
    else:
        row = db.get(Media, reference_id)
    if row:
        db.delete(row)


def _ensure_warehouse_quantities(db: Session, product: Product) -> None:
    warehouses = db.execute(select(Warehouse)).scalars().all()
    existing = {q.warehouse_id for q in product.quantities}
    for warehouse in warehouses:
        if warehouse.id not in existing:
            db.add(
                Quantity(
                    product_id=product.id,
                    warehouse_id=warehouse.id,
                    on_hand=0,
                    reserved=0,
                    ordered=0,
                    location=0,
                )
            )


def _resolve_product_category_id(db: Session, target_type: TargetType) -> int:
    name = TARGET_TO_PRODUCT_CATEGORY[target_type]
    category = db.execute(
        select(ProductCategory).where(ProductCategory.name == name)
    ).scalar_one_or_none()
    if not category:
        raise InvalidInputError(f"Product category '{name}' is not configured.")
    return category.id


def migrate_product_catalog_type(
    db: Session,
    product_id: int,
    *,
    target_type: TargetType,
    target_category_id: int,
    overrides: dict[str, Any] | None = None,
    child_overrides: dict[str, dict[str, Any]] | None = None,
) -> Product:
    product = db.execute(
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.category),
            selectinload(Product.quantities),
            selectinload(Product.air_filter),
            selectinload(Product.stock_item),
            selectinload(Product.media),
            selectinload(Product.child_products).selectinload(ChildProduct.air_filter),
            selectinload(Product.child_products).selectinload(ChildProduct.stock_item),
            selectinload(Product.child_products).selectinload(ChildProduct.media),
        )
    ).unique().scalar_one_or_none()

    if not product:
        raise ResourceNotFoundError("Product", product_id)
    if not product.is_active:
        raise InvalidInputError("Cannot migrate an archived product.")
    if not product.category:
        raise InvalidInputError("Product category is missing.")

    source_type = SOURCE_TYPE_FROM_CATEGORY.get(product.category.name)
    if not source_type:
        raise InvalidInputError(f"Unsupported source category '{product.category.name}'.")
    if source_type == target_type:
        raise InvalidInputError("Product is already in the target catalog type.")

    _validate_target_category(db, target_type, target_category_id)

    child_ids = [child.id for child in product.child_products]
    if _has_pending_transactions(db, product.id, child_ids):
        raise InvalidInputError(
            "Cannot migrate while pending inventory transactions exist. "
            "Cancel or commit them first."
        )

    parent_snapshot = _merge_overrides(_load_catalog_snapshot(product), overrides)
    _assert_identifier_available(db, target_type, parent_snapshot.identifier)

    old_parent_ref = product.reference_id
    old_parent_source = source_type

    new_parent_row = _create_target_row(
        db,
        target_type=target_type,
        snapshot=parent_snapshot,
        target_category_id=target_category_id,
    )
    product.category_id = _resolve_product_category_id(db, target_type)
    product.reference_id = new_parent_row.id

    child_old_refs: list[tuple[TargetType, int]] = []
    child_overrides = child_overrides or {}

    for child in product.child_products:
        child_source = _load_child_snapshot(child).source_type

        child_snapshot = _merge_overrides(
            _load_child_snapshot(child),
            child_overrides.get(str(child.id)) or child_overrides.get(child.id),
        )
        _assert_identifier_available(db, target_type, child_snapshot.identifier)

        new_child_row = _create_target_row(
            db,
            target_type=target_type,
            snapshot=child_snapshot,
            target_category_id=target_category_id,
        )
        child_old_refs.append((child_source, child.reference_id))
        child.category_id = product.category_id
        child.reference_id = new_child_row.id

    for child_source, child_ref in child_old_refs:
        _delete_source_row(db, child_source, child_ref)
    _delete_source_row(db, old_parent_source, old_parent_ref)

    _ensure_warehouse_quantities(db, product)
    db.flush()
    return product
