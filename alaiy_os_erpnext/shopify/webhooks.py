"""
Shopify webhook handler for alaiy OS ERPNext.
Route: /api/alaiy_os/shopify_webhook
Registered via hooks.py > website_route_rules.
"""
import hmac
import hashlib
import base64
import json
import frappe


def handle_webhook():
    """
    Entry point for all inbound Shopify webhooks.
    Validates HMAC signature, then routes by X-Shopify-Topic header.
    """
    request = frappe.local.request

    shared_secret = frappe.db.get_single_value("Shopify Settings", "shared_secret") or ""
    header_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")
    raw_body = request.get_data(as_text=False)

    if shared_secret and header_hmac:
        expected = base64.b64encode(
            hmac.new(shared_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        ).decode("utf-8")
        if not hmac.compare_digest(expected, header_hmac):
            frappe.throw("Shopify HMAC validation failed", frappe.AuthenticationError)

    topic = request.headers.get("X-Shopify-Topic", "")
    payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}

    if topic == "orders/create":
        _handle_order_create(payload)
    elif topic == "orders/paid":
        _handle_order_paid(payload)
    elif topic == "orders/cancelled":
        _handle_order_cancelled(payload)
    elif topic == "fulfillments/create":
        _handle_fulfillment_create(payload)
    elif topic == "fulfillments/update":
        _handle_fulfillment_update(payload)
    elif topic == "refunds/create":
        _handle_refund_create(payload)
    elif topic == "inventory_levels/update":
        _handle_inventory_update(payload)
    else:
        frappe.logger().info(f"Unhandled Shopify webhook topic: {topic}")

    frappe.db.commit()
    return {"status": "ok", "topic": topic}


def _handle_order_create(order):
    """Create a draft Sales Order in ERPNext from a Shopify order."""
    shopify_order_id = str(order.get("id", ""))
    if frappe.db.exists("Sales Order", {"custom_shopify_order_id": shopify_order_id}):
        return

    customer_name = _resolve_customer(order)

    so = frappe.new_doc("Sales Order")
    so.customer = customer_name
    so.transaction_date = frappe.utils.today()
    so.delivery_date = frappe.utils.add_days(frappe.utils.today(), 3)
    so.custom_shopify_order_id = shopify_order_id
    so.custom_channel = "shopify"

    for li in order.get("line_items", []):
        sku = li.get("sku") or li.get("title", "UNKNOWN")
        _ensure_item_exists(sku, li.get("title", sku))
        so.append("items", {
            "item_code": sku,
            "item_name": li.get("title", sku),
            "qty": float(li.get("quantity", 1)),
            "rate": float(li.get("price", 0)),
            "delivery_date": so.delivery_date,
        })

    if so.items:
        so.flags.ignore_mandatory = True
        so.insert(ignore_permissions=True)
        frappe.logger().info(f"Shopify webhook: created SO {so.name} for Shopify order {shopify_order_id}")


def _handle_order_paid(order):
    shopify_order_id = str(order.get("id", ""))
    so_name = frappe.db.get_value("Sales Order", {"custom_shopify_order_id": shopify_order_id})
    if so_name:
        frappe.db.set_value("Sales Order", so_name, "custom_payment_status", "Paid")


def _handle_order_cancelled(order):
    shopify_order_id = str(order.get("id", ""))
    so_name = frappe.db.get_value("Sales Order", {"custom_shopify_order_id": shopify_order_id})
    if so_name:
        so = frappe.get_doc("Sales Order", so_name)
        if so.docstatus == 1:
            so.cancel()
        elif so.docstatus == 0:
            frappe.delete_doc("Sales Order", so_name, ignore_permissions=True)


def _handle_fulfillment_create(fulfillment):
    order_id = str(fulfillment.get("order_id", ""))
    so_name = frappe.db.get_value("Sales Order", {"custom_shopify_order_id": order_id})
    if not so_name:
        return

    tracking_number = fulfillment.get("tracking_number", "")
    carrier = fulfillment.get("tracking_company", "")

    so = frappe.get_doc("Sales Order", so_name)
    dn = frappe.new_doc("Delivery Note")
    dn.customer = so.customer
    dn.company = so.company
    dn.posting_date = frappe.utils.today()
    dn.lr_no = tracking_number
    dn.lr_date = frappe.utils.today()
    dn.transporter_name = carrier
    dn.custom_shopify_fulfillment_id = str(fulfillment.get("id", ""))

    for item in so.items:
        if item.qty > item.delivered_qty:
            dn.append("items", {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": item.qty - item.delivered_qty,
                "rate": item.rate,
                "against_sales_order": so_name,
                "so_detail": item.name,
                "warehouse": item.warehouse,
            })

    if dn.items:
        dn.flags.ignore_mandatory = True
        dn.insert(ignore_permissions=True)
        dn.submit()


def _handle_fulfillment_update(fulfillment):
    fulfillment_id = str(fulfillment.get("id", ""))
    dn_name = frappe.db.get_value(
        "Delivery Note", {"custom_shopify_fulfillment_id": fulfillment_id}
    )
    if dn_name:
        frappe.db.set_value("Delivery Note", dn_name, "lr_no", fulfillment.get("tracking_number", ""))


def _handle_refund_create(refund):
    order_id = str(refund.get("order_id", ""))
    so_name = frappe.db.get_value("Sales Order", {"custom_shopify_order_id": order_id})
    if so_name:
        frappe.get_doc("Sales Order", so_name).add_comment(
            "Comment",
            text=f"Shopify refund created: {refund.get('id')} — {refund.get('note', '')}",
        )


def _handle_inventory_update(inventory_level):
    """No-op by default. Enable if Shopify should be the inventory master."""
    pass


def _resolve_customer(order):
    shopify_customer = order.get("customer") or {}
    full_name = (
        shopify_customer.get("first_name", "") + " " + shopify_customer.get("last_name", "")
    ).strip() or order.get("email", "Shopify Customer")

    existing = frappe.db.get_value("Customer", {"customer_name": full_name})
    if existing:
        return existing

    c = frappe.new_doc("Customer")
    c.customer_name = full_name
    c.customer_group = "Shopify"
    c.territory = "All Territories"
    c.flags.ignore_mandatory = True
    c.insert(ignore_permissions=True)
    return c.name


def _ensure_item_exists(item_code, item_name):
    if not frappe.db.exists("Item", item_code):
        item = frappe.new_doc("Item")
        item.item_code = item_code
        item.item_name = item_name or item_code
        item.item_group = "Shopify Items"
        item.is_stock_item = 1
        item.stock_uom = "Nos"
        item.flags.ignore_mandatory = True
        item.insert(ignore_permissions=True)
