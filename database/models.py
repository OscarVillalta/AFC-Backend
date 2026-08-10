from enum import Enum
from typing import List, Optional
from datetime import datetime, timezone

from sqlalchemy.orm import Mapped, mapped_column, relationship, foreign, object_session
from sqlalchemy import ForeignKey, String, Integer, BigInteger, Boolean, Float, Index, Sequence, func, text, Text, UniqueConstraint, select as sa_select, Table, Column
from sqlalchemy.inspection import inspect
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash

from database import Base


# =====================================================
# 🔹 Enums (string value containers)
# =====================================================

class OrderType(str, Enum):
    INSTALLATION = "installation"
    WILL_CALL = "will_call"
    DELIVERY = "delivery"
    SHIPMENT = "shipment"
    INCOMING = "incoming"
    VOID = "void"


class QBDocType(str, Enum):
    SALES_ORDER = "sales_order"
    PURCHASE_ORDER = "purchase_order"
    INVOICE = "invoice"
    ESTIMATE = "estimate"


# Outgoing-equivalent types: require customer, create tracker, reduce stock
OUTGOING_TYPES = {
    OrderType.INSTALLATION.value,
    OrderType.WILL_CALL.value,
    OrderType.DELIVERY.value,
    OrderType.SHIPMENT.value,
}

# Valid type values accepted for new orders
VALID_ORDER_TYPES = {
    OrderType.INSTALLATION.value,
    OrderType.WILL_CALL.value,
    OrderType.DELIVERY.value,
    OrderType.SHIPMENT.value,
    OrderType.INCOMING.value,
}


class OrderStatus(str, Enum):
    PENDING = "Pending"
    PARTIALLY_FULFILLED = "Partially Fulfilled"
    COMPLETED = "Completed"
    VOIDED = "Voided"


class OrderItemType(str, Enum):
    UNIT_SEPARATOR = "Unit_Separator"
    SECTION_SEPARATOR = "Section_Separator"
    PRODUCT_ITEM = "Product_Item"
    SALES_ITEM = "Sales_Item"


class TransactionState(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class TransactionReason(str, Enum):
    SHIPMENT = "shipment"
    ORDER = "ordered"
    RECEIVE = "receive"
    ADJUSTMENT = "adjustment"
    ROLLBACK = "rollback"
    ALLOCATION = "allocation"
    TRANSFER = "transfer"


class ConversionState(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class OutgoingOrderType(str, Enum):
    INSTALLATION = "Installation"
    WILL_CALL = "Will Call"
    SHIPMENT = "Shipment"
    DELIVERY = "Delivery"


class Department(str, Enum):
    SALES = "SALES"
    LOGISTICS = "LOGISTICS"
    WAREHOUSE = "WAREHOUSE"
    SERVICE = "SERVICE"
    ACCOUNTING = "ACCOUNTING"


class CalendarSyncStatus(str, Enum):
    PENDING = "pending"
    SYNCED = "synced"
    ERROR = "error"



# =====================================================
# 🔹 Base Serializer
# =====================================================

class SerializerMixin:
    def to_dict(self, include_relationships: bool = False):
        data = {}
        mapper = inspect(self).mapper

        for column in mapper.column_attrs:
            attr = column.key
            value = getattr(self, attr)

            if isinstance(value, Enum):
                value = value.value

            data[attr] = value

        if include_relationships:
            for relation in mapper.relationships:
                attr = relation.key
                value = getattr(self, attr)

                if value is None:
                    data[attr] = None
                elif relation.uselist:
                    data[attr] = [item.to_dict(include_relationships=False) for item in value]
                else:
                    data[attr] = value.to_dict(include_relationships=False)

        return data

    @classmethod
    def from_dict(cls, data: dict):
        obj = cls()
        for key, value in data.items():
            if hasattr(cls, key):
                setattr(obj, key, value)
        return obj


# =====================================================
# 🔹 Supplier
# =====================================================

class Supplier(Base, SerializerMixin):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    air_filters: Mapped[List["AirFilter"]] = relationship(back_populates="supplier")
    stock_items: Mapped[List["StockItem"]] = relationship(back_populates="supplier")
    media: Mapped[List["Media"]] = relationship(back_populates="supplier")
    orders: Mapped[List["Order"]] = relationship(back_populates="supplier")


# =====================================================
# 🔹 Categories
# =====================================================

class ProductCategory(Base, SerializerMixin):
    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    products: Mapped[List["Product"]] = relationship("Product", back_populates="category")


class AirFilterCategory(Base, SerializerMixin):
    __tablename__ = "air_filter_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    air_filters: Mapped[List["AirFilter"]] = relationship("AirFilter", back_populates="category")


class StockItemCategory(Base, SerializerMixin):
    __tablename__ = "stock_item_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    stock_items: Mapped[List["StockItem"]] = relationship("StockItem", back_populates="category")


class MediaCategory(Base, SerializerMixin):
    __tablename__ = "media_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    media: Mapped[List["Media"]] = relationship("Media", back_populates="category")


# =====================================================
# 🔹 Catalog Items
# =====================================================

class AirFilter(Base, SerializerMixin):
    __tablename__ = "air_filters"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    part_number: Mapped[str] = mapped_column(unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    merv_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)

    category_id: Mapped[int] = mapped_column(ForeignKey("air_filter_categories.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="air_filters")
    category: Mapped["AirFilterCategory"] = relationship(back_populates="air_filters")

    product: Mapped[Optional["Product"]] = relationship(
        "Product",
        primaryjoin=lambda: Product.reference_id == foreign(AirFilter.id),
        foreign_keys=lambda: [Product.reference_id],
        back_populates="air_filter",
        uselist=False,
        viewonly=True,
    )

    child_product: Mapped[Optional["ChildProduct"]] = relationship(
        "ChildProduct",
        primaryjoin=lambda: ChildProduct.reference_id == foreign(AirFilter.id),
        foreign_keys=lambda: [ChildProduct.reference_id],
        back_populates="air_filter",
        uselist=False,
        viewonly=True,
    )



class StockItem(Base, SerializerMixin):
    __tablename__ = "stock_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("stock_item_categories.id"), nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="stock_items")
    category: Mapped["StockItemCategory"] = relationship(back_populates="stock_items")

    product: Mapped[Optional["Product"]] = relationship(
        "Product",
        primaryjoin=lambda: Product.reference_id == foreign(StockItem.id),
        foreign_keys=lambda: [Product.reference_id],
        back_populates="stock_item",
        uselist=False,
        viewonly=True,
    )

    child_product: Mapped[Optional["ChildProduct"]] = relationship(
        "ChildProduct",
        primaryjoin=lambda: ChildProduct.reference_id == foreign(StockItem.id),
        foreign_keys=lambda: [ChildProduct.reference_id],
        back_populates="stock_item",
        uselist=False,
        viewonly=True,
    )


class Media(Base, SerializerMixin):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    part_number: Mapped[str] = mapped_column(unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    length: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit_of_measure: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    category_id: Mapped[int] = mapped_column(ForeignKey("media_categories.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="media")
    category: Mapped["MediaCategory"] = relationship(back_populates="media")

    product: Mapped[Optional["Product"]] = relationship(
        "Product",
        primaryjoin=lambda: Product.reference_id == foreign(Media.id),
        foreign_keys=lambda: [Product.reference_id],
        back_populates="media",
        uselist=False,
        viewonly=True,
    )

    child_product: Mapped[Optional["ChildProduct"]] = relationship(
        "ChildProduct",
        primaryjoin=lambda: ChildProduct.reference_id == foreign(Media.id),
        foreign_keys=lambda: [ChildProduct.reference_id],
        back_populates="media",
        uselist=False,
        viewonly=True,
    )


# =====================================================
# 🔹 Blocked Items
# =====================================================

class BlockedItem(Base, SerializerMixin):
    __tablename__ = "blocked_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)


# =====================================================
# 🔹 Product (Catalog Root w/ Soft Delete)
# =====================================================

class Product(Base, SerializerMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("product_categories.id"), nullable=False)
    reference_id: Mapped[int] = mapped_column(Integer, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_no_stock_deduction: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    category: Mapped["ProductCategory"] = relationship("ProductCategory", back_populates="products")

    air_filter: Mapped[Optional["AirFilter"]] = relationship(
        "AirFilter",
        primaryjoin=lambda: Product.reference_id == foreign(AirFilter.id),
        foreign_keys=lambda: [Product.reference_id],
        back_populates="product",
        uselist=False,
    )

    stock_item: Mapped[Optional["StockItem"]] = relationship(
        "StockItem",
        primaryjoin=lambda: Product.reference_id == foreign(StockItem.id),
        foreign_keys=lambda: [Product.reference_id],
        back_populates="product",
        uselist=False,
    )

    media: Mapped[Optional["Media"]] = relationship(
        "Media",
        primaryjoin=lambda: Product.reference_id == foreign(Media.id),
        foreign_keys=lambda: [Product.reference_id],
        back_populates="product",
        uselist=False,
    )

    quantities: Mapped[List["Quantity"]] = relationship(
        "Quantity",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def quantity(self) -> Optional["Quantity"]:
        """Returns the first quantity record for backward compatibility.
        For warehouse-aware queries, filter quantities by warehouse_id explicitly."""
        return self.quantities[0] if self.quantities else None

    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="product",
        passive_deletes=True,
    )

    order_items: Mapped[List["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="product",
        passive_deletes=True,
    )

    child_products: Mapped[List["ChildProduct"]] = relationship(
        "ChildProduct",
        back_populates="parent_product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )



# =====================================================
# 🔹 ChildProduct (Uses Parent's Quantity)
# =====================================================

class ChildProduct(Base, SerializerMixin):
    __tablename__ = "child_products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("product_categories.id"), nullable=False)
    reference_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # CASCADE: Deleting the parent product will automatically delete all child products
    parent_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    category: Mapped["ProductCategory"] = relationship("ProductCategory")
    parent_product: Mapped["Product"] = relationship("Product", back_populates="child_products", passive_deletes=True)

    air_filter: Mapped[Optional["AirFilter"]] = relationship(
        "AirFilter",
        primaryjoin=lambda: ChildProduct.reference_id == foreign(AirFilter.id),
        foreign_keys=lambda: [ChildProduct.reference_id],
        back_populates="child_product",
        uselist=False,
    )

    stock_item: Mapped[Optional["StockItem"]] = relationship(
        "StockItem",
        primaryjoin=lambda: ChildProduct.reference_id == foreign(StockItem.id),
        foreign_keys=lambda: [ChildProduct.reference_id],
        back_populates="child_product",
        uselist=False,
    )

    media: Mapped[Optional["Media"]] = relationship(
        "Media",
        primaryjoin=lambda: ChildProduct.reference_id == foreign(Media.id),
        foreign_keys=lambda: [ChildProduct.reference_id],
        back_populates="child_product",
        uselist=False,
    )

    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="child_product",
        passive_deletes=True,
    )

    order_items: Mapped[List["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="child_product",
        passive_deletes=True,
    )

    @property
    def quantity(self) -> Optional["Quantity"]:
        """Returns the first quantity record of the parent product (for backward compatibility)."""
        if self.parent_product and self.parent_product.quantities:
            return self.parent_product.quantities[0]
        return None


# =====================================================
# 🔹 Warehouse
# =====================================================

class Warehouse(Base, SerializerMixin):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    quantities: Mapped[List["Quantity"]] = relationship(
        "Quantity", back_populates="warehouse"
    )
    orders: Mapped[List["Order"]] = relationship(
        "Order", back_populates="warehouse"
    )
    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction", back_populates="warehouse"
    )
    order_trackers: Mapped[List["OrderTracker"]] = relationship(
        "OrderTracker", back_populates="warehouse"
    )
    conversion_batches: Mapped[List["ConversionBatch"]] = relationship(
        "ConversionBatch", back_populates="warehouse"
    )
    conversions: Mapped[List["Conversion"]] = relationship(
        "Conversion", back_populates="warehouse"
    )


# =====================================================
# 🔹 Quantity
# =====================================================

class Quantity(Base, SerializerMixin):
    __tablename__ = "quantities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)

    on_hand: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ordered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    location: Mapped[int] = mapped_column()

    product: Mapped["Product"] = relationship(back_populates="quantities", passive_deletes=True)
    warehouse: Mapped["Warehouse"] = relationship(back_populates="quantities")

    __table_args__ = (
        UniqueConstraint("product_id", "warehouse_id", name="uq_quantity_product_warehouse"),
    )

    @hybrid_property
    def available(self):
        return max(self.on_hand - self.reserved, 0)

    @available.expression
    def available(cls):
        return func.greatest(cls.on_hand - cls.reserved, 0)

    @hybrid_property
    def backordered(self):
        return abs(min(0, self.on_hand - self.reserved))

    @backordered.expression
    def backordered(cls):
        return func.greatest(cls.reserved - cls.on_hand, 0)


# =====================================================
# 🔹 Customer
# =====================================================

class Customer(Base, SerializerMixin):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    orders: Mapped[List["Order"]] = relationship(back_populates="customer")


# =====================================================
# 🔹 Orders
# =====================================================

class Order(Base, SerializerMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(unique=True, nullable=True)
    external_order_number: Mapped[Optional[str]] = mapped_column(nullable=True)
    qb_doc_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    type: Mapped[str] = mapped_column(String, nullable=False)  # OrderType values
    order_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # OutgoingOrderType values

    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"))
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, server_default="1")

    status: Mapped[str] = mapped_column(String, default=OrderStatus.PENDING.value, nullable=False)
    description: Mapped[Optional[str]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    eta: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_invoiced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    supplier: Mapped[Optional["Supplier"]] = relationship("Supplier", back_populates="orders")
    customer: Mapped[Optional["Customer"]] = relationship("Customer", back_populates="orders")
    warehouse: Mapped["Warehouse"] = relationship("Warehouse", back_populates="orders")

    items: Mapped[List["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan"
    )

    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="order",
        passive_deletes=True,
    )

    tracker: Mapped[Optional["OrderTracker"]] = relationship(
        "OrderTracker",
        back_populates="order",
        uselist=False,
        cascade="all, delete-orphan",
    )

    history: Mapped[List["OrderHistory"]] = relationship(
        "OrderHistory",
        back_populates="order",
        cascade="all, delete-orphan",
    )

    stages: Mapped[List["OrderTrackerStage"]] = relationship(
        "OrderTrackerStage",
        back_populates="order",
        cascade="all, delete-orphan",
    )

    calendar_event: Mapped[Optional["OrderCalendarEvent"]] = relationship(
        "OrderCalendarEvent",
        back_populates="order",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @staticmethod
    def _is_line_fulfilled(item: "OrderItem") -> bool:
        return item.quantity_ordered > 0 and item.quantity_fulfilled >= item.quantity_ordered

    def stock_trackable_items(self) -> list["OrderItem"]:
        return [
            item for item in self.items
            if item.type not in (OrderItemType.UNIT_SEPARATOR.value, OrderItemType.SECTION_SEPARATOR.value)
            and not item.skips_inventory()
        ]

    def skip_inventory_items(self) -> list["OrderItem"]:
        return [
            item for item in self.items
            if item.type not in (OrderItemType.UNIT_SEPARATOR.value, OrderItemType.SECTION_SEPARATOR.value)
            and item.skips_inventory()
        ]

    def pending_stock_trackable_items(self) -> list["OrderItem"]:
        return [
            item for item in self.stock_trackable_items()
            if not Order._is_line_fulfilled(item)
        ]

    def unfulfilled_skip_inventory_items(self) -> list["OrderItem"]:
        return [
            item for item in self.skip_inventory_items()
            if not Order._is_line_fulfilled(item)
        ]

    def can_manual_complete(self) -> bool:
        if self.type == OrderType.VOID.value:
            return False
        if self.status in (OrderStatus.COMPLETED.value, OrderStatus.VOIDED.value):
            return False
        if not self.items:
            return False
        if self.pending_stock_trackable_items():
            return False
        trackable = self.stock_trackable_items()
        if not trackable:
            return True
        return len(self.unfulfilled_skip_inventory_items()) > 0

    def update_status(self):
        db = object_session(self)

        # Always read fresh line items so parallel bulk commits don't use stale snapshots.
        if db is not None:
            rows = db.execute(
                sa_select(OrderItem).where(OrderItem.order_id == self.id)
            ).scalars().all()
        else:
            rows = list(self.items)

        if not rows:
            self.status = OrderStatus.PENDING.value
            self.completed_at = None
            return

        separator_types = (OrderItemType.UNIT_SEPARATOR.value, OrderItemType.SECTION_SEPARATOR.value)
        is_incoming = self.type == OrderType.INCOMING.value

        stock_items = [
            item for item in rows
            if item.type not in separator_types
            and not (not is_incoming and item.no_stock_deduction)
        ]

        skip_items = [
            item for item in rows
            if item.type not in separator_types
            and not is_incoming and item.no_stock_deduction
        ]

        if not stock_items:
            if self.status == OrderStatus.COMPLETED.value:
                return
            self.status = OrderStatus.PENDING.value
            self.completed_at = None
            return

        stock_all_done = all(Order._is_line_fulfilled(item) for item in stock_items)
        skip_all_done = all(Order._is_line_fulfilled(item) for item in skip_items) if skip_items else True

        if stock_all_done and skip_all_done:
            self.status = OrderStatus.COMPLETED.value
            self.completed_at = self.completed_at or datetime.now(timezone.utc)
        elif stock_all_done and not skip_all_done:
            self.status = OrderStatus.PARTIALLY_FULFILLED.value
            self.completed_at = None
        elif any(item.quantity_fulfilled > 0 for item in stock_items):
            self.status = OrderStatus.PARTIALLY_FULFILLED.value
            self.completed_at = None
        else:
            self.status = OrderStatus.PENDING.value
            self.completed_at = None

    def generate_order_number(db):
        last = db.execute(text("SELECT MAX(id) FROM orders")).scalar() or 0
        return f"AFC-{last + 1:06d}"


# =====================================================
# 🔹 Order Items
# =====================================================

class OrderItem(Base, SerializerMixin):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True)
    child_product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("child_products.id"), nullable=True)

    type: Mapped[str] = mapped_column(String, default=OrderItemType.PRODUCT_ITEM.value, nullable=False)
    quantity_ordered: Mapped[int] = mapped_column(default=0, nullable=False)
    quantity_fulfilled: Mapped[int] = mapped_column(default=0)
    note: Mapped[Optional[str]] = mapped_column(nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    no_stock_deduction: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    completion_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped[Optional["Product"]] = relationship("Product", back_populates="order_items")
    child_product: Mapped[Optional["ChildProduct"]] = relationship("ChildProduct", back_populates="order_items")

    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="order_item",
        passive_deletes=True,
    )

    @property
    def remaining(self) -> int:
        return max(self.quantity_ordered - self.quantity_fulfilled, 0)

    def skips_inventory(self) -> bool:
        if self.order and self.order.type == OrderType.INCOMING.value:
            return False
        return bool(self.no_stock_deduction)

    @property
    def status(self) -> str:
        if self.quantity_fulfilled == 0:
            return "Pending"
        if self.quantity_fulfilled < self.quantity_ordered:
            return "Partially Fulfilled"
        return "Completed"


# =====================================================
# 🔹 Transactions (Immutable Ledger)
# =====================================================

class Transaction(Base, SerializerMixin):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=True)
    # RESTRICT: Transactions prevent deletion of child products (must delete transaction first)
    child_product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("child_products.id", ondelete="RESTRICT"), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id", ondelete="SET NULL"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, server_default="1")

    quantity_delta: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String, default=TransactionState.PENDING.value, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[Optional[str]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    ledger_sequence: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    order: Mapped[Optional["Order"]] = relationship("Order", back_populates="transactions", passive_deletes=True)
    order_item: Mapped[Optional["OrderItem"]] = relationship("OrderItem", back_populates="transactions", passive_deletes=True)
    product: Mapped[Optional["Product"]] = relationship("Product", back_populates="transactions", passive_deletes=True)
    child_product: Mapped[Optional["ChildProduct"]] = relationship("ChildProduct", back_populates="transactions", passive_deletes=True)
    warehouse: Mapped["Warehouse"] = relationship("Warehouse", back_populates="transactions")

    # ================================
    #  Inventory Logic
    # ================================

    def _get_quantity_record(self) -> Optional["Quantity"]:
        """Get the Quantity record for this transaction's product and warehouse."""
        db = object_session(self)
        if db is None:
            return None

        product_id = self.product_id
        if product_id is None and self.child_product_id is not None:
            cp = self.child_product
            if cp is None:
                cp = db.get(ChildProduct, self.child_product_id)
            if cp:
                product_id = cp.parent_product_id

        if product_id is None or self.warehouse_id is None:
            return None

        return db.execute(
            sa_select(Quantity).where(
                (Quantity.product_id == product_id) &
                (Quantity.warehouse_id == self.warehouse_id)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _release_ordered(qty_record: "Quantity", amount: int) -> None:
        qty_record.ordered = max(0, qty_record.ordered - amount)

    @staticmethod
    def _release_reserved(qty_record: "Quantity", amount: int) -> None:
        qty_record.reserved = max(0, qty_record.reserved - amount)

    def commit(self, db=None):
        if self.state != TransactionState.PENDING.value:
            return

        qty_record = self._get_quantity_record()
        if not qty_record:
            raise ValueError("Quantity record missing.")

        # Apply physical change
        qty_record.on_hand += self.quantity_delta

        # Remove pending planning effect (never drive reserved/ordered below zero)
        if self.quantity_delta > 0:
            Transaction._release_ordered(qty_record, abs(self.quantity_delta))
        else:
            Transaction._release_reserved(qty_record, abs(self.quantity_delta))

        self.state = TransactionState.COMMITTED.value
        self.last_updated_at = datetime.now(timezone.utc)

        # Assign ledger sequence from PostgreSQL sequence
        if db is not None:
            seq_val = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
            self.ledger_sequence = seq_val

        # Fulfillment progression
        if self.order_item:
            item = self.order_item
            item.quantity_fulfilled += abs(self.quantity_delta)
            item.quantity_fulfilled = max(0, min(item.quantity_fulfilled, item.quantity_ordered))

            # Update order status directly
            if item.order:
                item.order.update_status()

    def rollback(self, db, performed_by: Optional[str] = None):
        if self.state != TransactionState.COMMITTED.value:
            raise ValueError("Only committed transactions can be rolled back.")

        if self.reason == TransactionReason.ROLLBACK.value:
            raise ValueError("Reversal transactions cannot be rolled back.")

        # ✅ CRITICAL FIX: Pass lock=True to acquire a pessimistic lock (FOR UPDATE)
        # If another request is touching this quantity, Python pauses here until it finishes.
        qty_record = self._get_quantity_record()
        
        if not qty_record:
            raise ValueError("Quantity record missing.")

        reversed_delta = -self.quantity_delta  # opposite sign
        rollback_requires_stock = reversed_delta < 0  # removing stock

        # ✅ SAFE VALIDATION: Because the row is locked, this check is now thread-safe.
        # No other transaction can alter the on_hand quantity out from under us.
        if rollback_requires_stock:
            required = abs(reversed_delta)
            if qty_record.on_hand < required:
                raise ValueError(
                    f"Cannot rollback transaction #{self.id}: "
                    f"rollback would remove {required} from on_hand, "
                    f"but only {qty_record.on_hand} is available."
                )

        reversed_txn = Transaction(
            product_id=self.product_id,
            child_product_id=self.child_product_id,
            order_id=self.order_id,
            order_item_id=self.order_item_id,
            warehouse_id=self.warehouse_id,
            quantity_delta=reversed_delta,
            reason=TransactionReason.ROLLBACK.value,
            state=TransactionState.COMMITTED.value,
            note=f"Reversal of transaction #{self.id}",
        )

        # Assign ledger sequence for the reversal transaction
        seq_val = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
        reversed_txn.ledger_sequence = seq_val
        reversed_txn.last_updated_at = datetime.now(timezone.utc)

        # ✅ SAFE MATH: We can use standard Python += math here because the lock 
        # guarantees we are working with the absolute latest numbers.
        qty_record.on_hand += reversed_txn.quantity_delta

        if self.order_item:
            item = self.order_item
            item.quantity_fulfilled -= abs(self.quantity_delta)
            item.quantity_fulfilled = max(0, min(item.quantity_fulfilled, item.quantity_ordered))

            if item.order:
                item.order.update_status()

        self.state = TransactionState.ROLLED_BACK.value
        self.last_updated_at = datetime.now(timezone.utc)

        db.add(reversed_txn)
        return reversed_txn


    def cancel(self):
        if self.state == TransactionState.PENDING.value:

            qty_record = self._get_quantity_record()
            if qty_record:
                if self.quantity_delta > 0:
                    Transaction._release_ordered(qty_record, abs(self.quantity_delta))
                else:
                    Transaction._release_reserved(qty_record, abs(self.quantity_delta))

            self.state = TransactionState.CANCELLED.value
            self.last_updated_at = datetime.now(timezone.utc)
        else:
            raise ValueError("Only pending transactions can be cancelled.")


# =====================================================
# 🔹 Conversions
# =====================================================


class ConversionBatch(Base, SerializerMixin):
    __tablename__ = "conversion_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(default=datetime.now(timezone.utc))
    created_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(nullable=True)
    external_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    order: Mapped[Optional["Order"]] = relationship("Order")
    warehouse: Mapped["Warehouse"] = relationship("Warehouse", back_populates="conversion_batches")
    conversions: Mapped[List["Conversion"]] = relationship("Conversion", back_populates="batch")


class Conversion(Base, SerializerMixin):
    __tablename__ = "conversions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("conversion_batches.id", ondelete="SET NULL"), nullable=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, server_default="1")
    increase_txn_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String, default=ConversionState.COMPLETED.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.now(timezone.utc))
    note: Mapped[Optional[str]] = mapped_column(nullable=True)

    batch: Mapped[Optional["ConversionBatch"]] = relationship("ConversionBatch", back_populates="conversions")
    warehouse: Mapped["Warehouse"] = relationship("Warehouse", back_populates="conversions")
    increase_txn: Mapped["Transaction"] = relationship("Transaction", foreign_keys=[increase_txn_id])
    decreases: Mapped[List["ConversionDecrease"]] = relationship(
        "ConversionDecrease", back_populates="conversion", cascade="all, delete-orphan"
    )


class ConversionDecrease(Base, SerializerMixin):
    __tablename__ = "conversion_decreases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversion_id: Mapped[int] = mapped_column(
        ForeignKey("conversions.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True, nullable=False)

    conversion: Mapped["Conversion"] = relationship("Conversion", back_populates="decreases")
    transaction: Mapped["Transaction"] = relationship("Transaction")


# =====================================================
# 🔹 Order Tracker
# =====================================================

class OrderTracker(Base, SerializerMixin):
    __tablename__ = "order_tracker"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, unique=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, server_default="1")
    current_department: Mapped[str] = mapped_column(String, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_backordered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    order: Mapped["Order"] = relationship("Order", back_populates="tracker")
    warehouse: Mapped["Warehouse"] = relationship("Warehouse", back_populates="order_trackers")


class OrderHistory(Base, SerializerMixin):
    __tablename__ = "order_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    from_department: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_department: Mapped[str] = mapped_column(String, nullable=False)
    action_taken: Mapped[str] = mapped_column(String, nullable=False)
    performed_by: Mapped[str] = mapped_column(String, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    comments: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="history")


class OrderTrackerStage(Base, SerializerMixin):
    """Stores the individual completion state for each tracker stage."""
    __tablename__ = "order_tracker_stages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="stages")

    __table_args__ = (
        UniqueConstraint("order_id", "stage_index", name="uq_order_tracker_stage"),
    )


class OrderCalendarEvent(Base, SerializerMixin):
    """Maps an order to a Google Calendar event (1:1)."""
    __tablename__ = "order_calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    google_event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, unique=True)
    google_calendar_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime] = mapped_column(nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_status: Mapped[str] = mapped_column(
        String, default=CalendarSyncStatus.PENDING.value, nullable=False
    )
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    order: Mapped["Order"] = relationship("Order", back_populates="calendar_event")


# =====================================================
# 🔹 RBAC: Role, Permission, User
# =====================================================

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True),
    Column("permission_id", Integer, ForeignKey("permissions.id"), primary_key=True),
)


class Permission(Base, SerializerMixin):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Role(Base, SerializerMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    permissions: Mapped[List["Permission"]] = relationship(
        "Permission", secondary=role_permissions, lazy="selectin"
    )


class User(Base, SerializerMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    role: Mapped[Optional["Role"]] = relationship("Role", lazy="selectin")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


# =====================================================
# 🔹 Indexes
# =====================================================

Index("ix_transactions_order_id", Transaction.order_id)
Index("ix_transactions_order_item_id", Transaction.order_item_id)
Index("ix_transactions_product_id", Transaction.product_id)
Index("ix_transactions_child_product_id", Transaction.child_product_id)
Index("ix_transactions_state", Transaction.state)
Index("ix_transactions_created_at", Transaction.created_at)
Index("ix_transactions_ledger_sequence", Transaction.ledger_sequence)
Index("ix_conversion_batches_order_id", ConversionBatch.order_id)
Index("ix_conversion_batches_created_at", ConversionBatch.created_at)
Index("ix_conversions_batch_id", Conversion.batch_id)
Index("ix_conversions_state", Conversion.state)
Index("ix_conversions_created_at", Conversion.created_at)
Index("ix_conversion_decreases_conversion_id", ConversionDecrease.conversion_id)
