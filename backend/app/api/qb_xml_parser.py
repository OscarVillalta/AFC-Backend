"""
Helper module to parse QuickBooks XML responses.
"""
import defusedxml.ElementTree as ET
from typing import List, Dict, Optional


def parse_qb_line_items(qbxml_response: str, entity_type: str) -> List[Dict]:
    """
    Parse QuickBooks XML response and extract line items.
    
    Args:
        qbxml_response: The raw QBXML response string
        entity_type: Type of QB document ('sales_order', 'estimate', 'invoice')
    
    Returns:
        List of dictionaries containing line item data
    """
    if not qbxml_response:
        return []
    
    try:
        root = ET.fromstring(qbxml_response)
    except ET.ParseError as e:
        raise ValueError(f"Failed to parse QBXML: {str(e)}")
    
    # Map entity types to their response tags
    entity_map = {
        'sales_order': ('SalesOrderRet', 'SalesOrderLineRet'),
        'salesorder': ('SalesOrderRet', 'SalesOrderLineRet'),
        'estimate': ('EstimateRet', 'EstimateLineRet'),
        'invoice': ('InvoiceRet', 'InvoiceLineRet'),
        'purchase_order': ('PurchaseOrderRet', 'PurchaseOrderLineRet'),
        'purchaseorder': ('PurchaseOrderRet', 'PurchaseOrderLineRet'),
    }
    
    entity_type_lower = entity_type.lower()
    if entity_type_lower not in entity_map:
        raise ValueError(f"Unsupported entity type: {entity_type}")
    
    ret_tag, line_tag = entity_map[entity_type_lower]
    
    # Find the response element
    ret_element = root.find(f".//{ret_tag}")
    if ret_element is None:
        # Try with namespace prefix
        for elem in root.iter():
            if elem.tag.endswith(ret_tag):
                ret_element = elem
                break
    
    if ret_element is None:
        return []
    
    # Extract line items
    line_items = []
    
    # Find all line item elements
    for line_elem in ret_element.findall(f".//{line_tag}"):
        line_item = parse_line_item(line_elem)
        if line_item:
            line_items.append(line_item)
    
    # Also check for LineGroupRet (sections/groups) - entity-specific
    group_tag_map = {
        'sales_order': 'SalesOrderLineGroupRet',
        'salesorder': 'SalesOrderLineGroupRet',
        'estimate': 'EstimateLineGroupRet',
        'invoice': 'InvoiceLineGroupRet',
        'purchase_order': 'PurchaseOrderLineGroupRet',
        'purchaseorder': 'PurchaseOrderLineGroupRet',
    }
    
    group_tag = group_tag_map.get(entity_type_lower)
    if group_tag:
        for group_elem in ret_element.findall(f".//{group_tag}"):
            # Group header can act as a separator
            group_desc = get_element_text(group_elem, "Desc")
            if group_desc:
                line_items.append({
                    'is_separator': True,
                    'description': group_desc,
                    'name': None,
                    'quantity': 0
                })
            
            # Lines within the group
            for line_elem in group_elem.findall(f".//{line_tag}"):
                line_item = parse_line_item(line_elem)
                if line_item:
                    line_items.append(line_item)
    
    return line_items


def parse_line_item(line_elem: ET.Element) -> Optional[Dict]:
    """
    Parse a single line item element.
    
    Returns:
        Dictionary with line item data, or None if invalid
    """
    # Get item reference (name)
    item_name = None
    item_ref = line_elem.find(".//ItemRef")
    if item_ref is not None:
        full_name = item_ref.find("FullName")
        if full_name is not None:
            item_name = full_name.text
    
    # Get description
    desc = get_element_text(line_elem, "Desc")
    
    # Get quantity
    qty_text = get_element_text(line_elem, "Quantity")
    quantity = 0
    if qty_text:
        try:
            quantity = float(qty_text)
            # Ensure non-negative
            if quantity < 0:
                quantity = 0
        except (ValueError, TypeError):
            quantity = 0
    
    # If no item name but has description, treat as separator
    if not item_name and desc:
        return {
            'is_separator': True,
            'description': desc,
            'name': None,
            'quantity': 0
        }
    
    # If has item name, it's a product line
    if item_name:
        return {
            'is_separator': False,
            'name': item_name,
            'description': desc,
            'quantity': int(quantity) if quantity == int(quantity) else quantity
        }
    
    # Skip empty lines
    return None


def get_element_text(parent: ET.Element, tag_name: str) -> Optional[str]:
    """Get text content of a child element."""
    elem = parent.find(f".//{tag_name}")
    if elem is not None and elem.text:
        return elem.text.strip()
    return None


def extract_qb_metadata(qbxml_response: str, entity_type: str) -> Dict:
    """
    Extract metadata from QuickBooks response (ref number, customer, etc.)
    
    Args:
        qbxml_response: The raw QBXML response string
        entity_type: Type of QB document ('sales_order', 'estimate', 'invoice')
    
    Returns:
        Dictionary with metadata
    """
    if not qbxml_response:
        return {}
    
    try:
        root = ET.fromstring(qbxml_response)
    except ET.ParseError:
        return {}
    
    # Map entity types to their response tags
    entity_map = {
        'sales_order': 'SalesOrderRet',
        'salesorder': 'SalesOrderRet',
        'estimate': 'EstimateRet',
        'invoice': 'InvoiceRet',
        'purchase_order': 'PurchaseOrderRet',
        'purchaseorder': 'PurchaseOrderRet',
    }
    
    entity_type_lower = entity_type.lower()
    ret_tag = entity_map.get(entity_type_lower)
    if not ret_tag:
        return {}
    
    # Find the response element
    ret_element = root.find(f".//{ret_tag}")
    if ret_element is None:
        for elem in root.iter():
            if elem.tag.endswith(ret_tag):
                ret_element = elem
                break
    
    if ret_element is None:
        return {}
    
    metadata = {}
    
    # Extract reference number
    ref_number = get_element_text(ret_element, "RefNumber")
    if ref_number:
        metadata['ref_number'] = ref_number
    
    # Extract customer name (sales orders, estimates, invoices)
    customer_ref = ret_element.find(".//CustomerRef")
    if customer_ref is not None:
        customer_name = get_element_text(customer_ref, "FullName")
        if customer_name:
            metadata['customer_name'] = customer_name
    
    # Extract vendor name (purchase orders)
    vendor_ref = ret_element.find(".//VendorRef")
    if vendor_ref is not None:
        vendor_name = get_element_text(vendor_ref, "FullName")
        if vendor_name:
            metadata['vendor_name'] = vendor_name
    
    # Get creation date
    creation_date = get_element_text(ret_element, "TimeCreated")
    print(creation_date, flush=True)
    if creation_date:
        metadata['created_at'] = creation_date
    
    # Extract ETA (ExpectedDate for purchase orders)
    expected_date = get_element_text(ret_element, "ShipDate")
    if expected_date:
        metadata['eta'] = expected_date
    
    # Extract memo/description
    memo = get_element_text(ret_element, "Memo")
    if memo:
        metadata['memo'] = memo
    
    return metadata
