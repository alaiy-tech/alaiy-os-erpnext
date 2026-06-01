import frappe
from frappe import _
import json
from datetime import datetime, timedelta


@frappe.whitelist()
def get_feed_items():
    """Returns all open/snoozed Marketplace Alert rows sorted by severity."""
    now = frappe.utils.now_datetime()
    severity_order = {"fire": 1, "warn": 2, "info": 3}
    alerts = frappe.get_all(
        "Marketplace Alert",
        filters=[["status", "in", ["Open", "Snoozed"]]],
        fields=[
            "name", "alert_type", "severity", "title", "description",
            "item_code", "sales_order", "marketplace", "status",
            "snoozed_until", "ai_note", "creation",
        ],
        order_by="creation desc",
        limit=50,
    )
    result = []
    for a in alerts:
        if a.status == "Snoozed" and a.snoozed_until and a.snoozed_until > now:
            continue
        result.append(a)
    result.sort(key=lambda x: (severity_order.get(x.get("severity", "info"), 3), str(x.get("creation", ""))))
    return result


@frappe.whitelist()
def get_kpi_summary():
    """Returns KPI summary for the dashboard header strip."""
    today = frappe.utils.today()
    yesterday = frappe.utils.add_days(today, -1)

    orders_today = frappe.db.count("Sales Order", {"transaction_date": today, "docstatus": 1})
    orders_yesterday = frappe.db.count("Sales Order", {"transaction_date": yesterday, "docstatus": 1})

    revenue_today = frappe.db.sql("""
        SELECT IFNULL(SUM(grand_total), 0) FROM `tabSales Order`
        WHERE transaction_date = %s AND docstatus = 1
    """, today)[0][0]

    revenue_yesterday = frappe.db.sql("""
        SELECT IFNULL(SUM(grand_total), 0) FROM `tabSales Order`
        WHERE transaction_date = %s AND docstatus = 1
    """, yesterday)[0][0]

    fires = frappe.db.count("Marketplace Alert", {"severity": "fire", "status": "Open"})
    warns = frappe.db.count("Marketplace Alert", {"severity": "warn", "status": "Open"})

    wh_stock = frappe.db.sql("""
        SELECT IFNULL(SUM(actual_qty), 0) FROM `tabBin` WHERE actual_qty > 0
    """)[0][0]

    sku_count = frappe.db.sql("""
        SELECT COUNT(DISTINCT item_code) FROM `tabBin` WHERE actual_qty > 0
    """)[0][0]

    health = frappe.db.get_value(
        "Account Health Snapshot",
        {"marketplace": "amazon_in"},
        ["metric_1_value", "metric_1_status", "metric_1_name"],
        order_by="snapshot_date desc",
        as_dict=True,
    )

    return {
        "orders_today": int(orders_today),
        "orders_yesterday": int(orders_yesterday),
        "revenue_today": float(revenue_today),
        "revenue_yesterday": float(revenue_yesterday),
        "items_to_action": int(fires + warns),
        "fires": int(fires),
        "warns": int(warns),
        "wh_stock": int(wh_stock),
        "sku_count": int(sku_count),
        "account_health": health or {},
    }


@frappe.whitelist()
def get_inventory_with_velocity():
    """Returns inventory table with stock, velocity, days cover per SKU per warehouse."""
    bins = frappe.db.sql("""
        SELECT
            b.item_code,
            i.item_name,
            b.warehouse,
            b.actual_qty,
            b.reserved_qty,
            (b.actual_qty - b.reserved_qty) AS available_qty
        FROM `tabBin` b
        JOIN `tabItem` i ON i.name = b.item_code
        WHERE b.actual_qty > 0
        ORDER BY b.item_code
    """, as_dict=True)

    fourteen_days_ago = frappe.utils.add_days(frappe.utils.today(), -14)
    velocity_data = frappe.db.sql("""
        SELECT
            soi.item_code,
            SUM(soi.qty) / 14.0 AS daily_velocity
        FROM `tabSales Order Item` soi
        JOIN `tabSales Order` so ON so.name = soi.parent
        WHERE so.transaction_date >= %s AND so.docstatus = 1
        GROUP BY soi.item_code
    """, fourteen_days_ago, as_dict=True)

    velocity_map = {v.item_code: v.daily_velocity for v in velocity_data}

    result = []
    for b in bins:
        velocity = velocity_map.get(b.item_code, 0)
        days_cover = round(b.available_qty / velocity, 1) if velocity > 0 else 999

        reorder_policy = frappe.db.get_value(
            "Reorder Policy",
            {"item_code": b.item_code, "warehouse": b.warehouse},
            ["reorder_point", "safety_stock"],
        )

        if reorder_policy:
            rp, ss = reorder_policy
            if b.actual_qty <= (ss or 0):
                status = "Low"
            elif b.actual_qty <= (rp or 0):
                status = "Reorder"
            else:
                status = "Healthy"
        else:
            if days_cover < 5:
                status = "Low"
            elif days_cover < 14:
                status = "Reorder"
            else:
                status = "Healthy"

        channel_skus = frappe.get_all(
            "Ecommerce Item",
            filters={"erpnext_item_code": b.item_code},
            fields=["integration", "sku"],
        )
        channels = [c.integration for c in channel_skus]

        result.append({
            **b,
            "daily_velocity": round(velocity, 1),
            "days_cover": days_cover,
            "status": status,
            "channels": channels,
        })

    return result


@frappe.whitelist()
def snooze_alert(alert_id, hours=24):
    """Snooze a Marketplace Alert for N hours."""
    hours = int(hours)
    snoozed_until = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=hours)
    frappe.set_value("Marketplace Alert", alert_id, {
        "status": "Snoozed",
        "snoozed_until": snoozed_until,
    })
    frappe.db.commit()
    return {"snoozed_until": str(snoozed_until)}


@frappe.whitelist()
def approve_reorder(item_code, qty, supplier=None):
    """Create a Draft Purchase Order for a reorder."""
    qty = float(qty)

    policy = frappe.db.get_value(
        "Reorder Policy",
        {"item_code": item_code},
        ["preferred_supplier", "unit_cost_cny", "unit_cost_inr", "warehouse", "lead_time_days", "reorder_qty"],
        as_dict=True,
    )

    supplier = supplier or (policy.preferred_supplier if policy else None)
    unit_rate = float(policy.unit_cost_inr or 0) if policy else 0
    lead_time = int(policy.lead_time_days or 14) if policy else 14

    po = frappe.new_doc("Purchase Order")
    po.supplier = supplier
    po.schedule_date = frappe.utils.add_days(frappe.utils.today(), lead_time)
    po.currency = "INR"
    po.append("items", {
        "item_code": item_code,
        "qty": qty,
        "rate": unit_rate,
        "schedule_date": po.schedule_date,
        "warehouse": policy.warehouse if policy else None,
    })
    po.flags.ignore_mandatory = True
    po.insert()

    existing_alert = frappe.db.get_value(
        "Marketplace Alert",
        {"item_code": item_code, "alert_type": "Reorder", "status": ["in", ["Open", "Snoozed"]]},
    )
    if existing_alert:
        frappe.set_value("Marketplace Alert", existing_alert, {
            "status": "Resolved",
            "action_taken": f"Purchase Order {po.name} created for {qty} units",
            "resolved_at": frappe.utils.now_datetime(),
        })

    frappe.db.commit()
    return {"purchase_order": po.name, "status": "created"}


@frappe.whitelist()
def mark_dispatched(sales_order_ids, carrier, awb_number, notify_customer=True):
    """Submit a Delivery Note with tracking for one or more Sales Orders."""
    if isinstance(sales_order_ids, str):
        sales_order_ids = json.loads(sales_order_ids)

    created = []
    for so_id in sales_order_ids:
        so = frappe.get_doc("Sales Order", so_id)

        dn = frappe.new_doc("Delivery Note")
        dn.customer = so.customer
        dn.company = so.company
        dn.posting_date = frappe.utils.today()
        dn.lr_no = awb_number
        dn.lr_date = frappe.utils.today()
        dn.transporter_name = carrier

        for item in so.items:
            if item.qty > item.delivered_qty:
                dn.append("items", {
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "qty": item.qty - item.delivered_qty,
                    "rate": item.rate,
                    "against_sales_order": so_id,
                    "so_detail": item.name,
                    "warehouse": item.warehouse,
                })

        if not dn.items:
            continue

        dn.flags.ignore_mandatory = True
        dn.insert()
        dn.submit()
        created.append(dn.name)

        existing_alert = frappe.db.get_value(
            "Marketplace Alert",
            {"sales_order": so_id, "alert_type": "Late Shipment", "status": ["in", ["Open", "Snoozed"]]},
        )
        if existing_alert:
            frappe.set_value("Marketplace Alert", existing_alert, {
                "status": "Resolved",
                "action_taken": f"Delivery Note {dn.name} created, AWB: {awb_number}",
                "resolved_at": frappe.utils.now_datetime(),
            })

    frappe.db.commit()
    return {"delivery_notes": created, "awb": awb_number}


@frappe.whitelist()
def amazon_get_orders(days=1):
    """Pull recent Amazon orders via SP API."""
    from alaiy_os_erpnext.amazon.sp_api import get_sp_client
    sp = get_sp_client()
    return sp.get_orders(days_ago=int(days))


@frappe.whitelist()
def amazon_reprice(sku, new_price):
    """Reprice an Amazon listing via SP API patchListingsItem."""
    from alaiy_os_erpnext.amazon.sp_api import get_sp_client
    sp = get_sp_client()
    return sp.reprice_listing(sku=sku, price=float(new_price))


@frappe.whitelist()
def amazon_get_account_health():
    """Get latest Amazon account health metrics."""
    from alaiy_os_erpnext.amazon.sp_api import get_sp_client
    sp = get_sp_client()
    return sp.get_account_health()


@frappe.whitelist()
def shopify_get_orders(limit=25):
    """Pull recent Shopify orders via GraphQL."""
    from alaiy_os_erpnext.shopify.graphql import ShopifyGraphQL
    shopify = ShopifyGraphQL()
    return shopify.get_orders(limit=int(limit))


@frappe.whitelist()
def shopify_fulfill_order(order_id, carrier, awb_number, notify_customer=True):
    """Fulfill a Shopify order via GraphQL fulfillmentCreate."""
    from alaiy_os_erpnext.shopify.graphql import ShopifyGraphQL
    shopify = ShopifyGraphQL()
    return shopify.fulfill_order(
        order_id=order_id,
        carrier=carrier,
        tracking_number=awb_number,
        notify_customer=bool(notify_customer),
    )


@frappe.whitelist()
def shopify_reprice(product_id, variant_id, price, compare_at_price=None):
    """Update price on a Shopify product variant."""
    from alaiy_os_erpnext.shopify.graphql import ShopifyGraphQL
    shopify = ShopifyGraphQL()
    return shopify.update_variant_price(
        product_id=product_id,
        variant_id=variant_id,
        price=str(price),
        compare_at_price=str(compare_at_price) if compare_at_price else None,
    )


@frappe.whitelist()
def shopify_update_inventory(inventory_item_id, location_id, qty):
    """Set absolute inventory quantity on Shopify."""
    from alaiy_os_erpnext.shopify.graphql import ShopifyGraphQL
    shopify = ShopifyGraphQL()
    return shopify.set_inventory(
        inventory_item_id=inventory_item_id,
        location_id=location_id,
        quantity=int(qty),
    )


@frappe.whitelist()
def get_account_health_latest():
    """Get latest Account Health Snapshot for all marketplaces."""
    return frappe.db.sql("""
        SELECT * FROM `tabAccount Health Snapshot`
        WHERE (marketplace, snapshot_date) IN (
            SELECT marketplace, MAX(snapshot_date)
            FROM `tabAccount Health Snapshot`
            GROUP BY marketplace
        )
        ORDER BY marketplace
    """, as_dict=True)


@frappe.whitelist()
def ask_alaiy(question):
    """Answer a natural language question about the brand's ops data from ERPNext."""
    import re
    q = (question or "").lower()

    # ── Reorder / stock ─────────────────────────────────────────
    if re.search(r"reorder|stock|inventory|low", q):
        items = get_inventory_with_velocity()
        low = [i for i in items if (i.get("days_cover") or 999) <= 14]
        if not low:
            return {"answer": "No SKUs are below the 14-day reorder threshold right now."}
        lines = []
        for i in low:
            lines.append(f"• {i['item_code']} — {i.get('actual_qty', 0)} units, {i.get('days_cover', '?')}d cover")
        return {"answer": "SKUs needing reorder:\n" + "\n".join(lines)}

    # ── Late shipment ─────────────────────────────────────────────
    if re.search(r"late|shipment|ship today|dispatch", q):
        today = frappe.utils.today()
        orders = frappe.get_all(
            "Sales Order",
            filters=[["delivery_date", "<=", today], ["status", "in", ["To Deliver and Bill", "To Deliver"]]],
            fields=["name", "delivery_date", "customer"],
            limit=10,
        )
        if not orders:
            return {"answer": "No orders at late-shipment risk today."}
        lines = [
            f"• {o['name']} — {o['customer']} (due {o['delivery_date']})".replace("None", "?")
            for o in orders
        ]
        return {"answer": f"{len(orders)} order(s) at late-shipment risk:\n" + "\n".join(lines)}

    # ── Return rate ─────────────────────────────────────────────
    if re.search(r"return|refund", q):
        week_ago = frappe.utils.add_days(frappe.utils.today(), -7)
        total = frappe.db.count("Sales Order", {"transaction_date": [">=", week_ago]}) or 1
        returned = frappe.db.count("Sales Order", {
            "transaction_date": [">=", week_ago],
            "status": "Cancelled",
        }) or 0
        rate = round(returned / total * 100, 1)
        return {"answer": f"Return/cancel rate this week: {rate}% ({returned} of {total} orders)."}

    # ── Revenue ─────────────────────────────────────────────────
    if re.search(r"revenue|sales|today", q):
        kpi = get_kpi_summary()
        today_rev = kpi.get("revenue_today", 0)
        yest_rev = kpi.get("revenue_yesterday", 0)
        pct = round((today_rev - yest_rev) / yest_rev * 100, 1) if yest_rev else 0
        direction = "↑" if pct >= 0 else "↓"
        return {
            "answer": (
                f"Today: ₹{today_rev:,.0f} ({kpi.get('orders_today', 0)} orders)\n"
                f"Yesterday: ₹{yest_rev:,.0f}\n"
                f"{direction} {abs(pct)}% vs yesterday"
            )
        }

    # ── Account health ───────────────────────────────────────────
    if re.search(r"health|odr|account", q):
        rows = get_account_health_latest()
        if not rows:
            return {"answer": "No account health data yet. Run the daily health poller first."}
        r = rows[0]
        lines = []
        for i in range(1, 5):
            name = r.get(f"metric_{i}_name")
            val = r.get(f"metric_{i}_value")
            status = r.get(f"metric_{i}_status", "ok")
            if name and val is not None:
                icon = "✓" if status == "ok" else "⚠"
                lines.append(f"{icon} {name}: {val:.1f}% ({status})")
        marketplace = r.get("marketplace") or "Amazon"
        return {"answer": f"Account health ({marketplace}):\n" + "\n".join(lines)}

    # ── Generic fallback ─────────────────────────────────────────
    return {
        "answer": (
            f'I couldn\'t find specific data for "{question}". '
            'Try: "Which SKUs need reorder?", "Revenue today", '
            '"Late shipment risk", "Return rate", "Account health".'
        ),
    }


@frappe.whitelist(allow_guest=True)
def shopify_oauth_callback():
    """
    Shopify OAuth callback — receives ?code= redirect, exchanges for shpat_ token,
    saves to site_config.json. Redirect URI: http://localhost:8080/api/method/alaiy_os_erpnext.api.shopify_oauth_callback
    """
    import urllib.request
    import urllib.parse

    code = frappe.request.args.get("code", "")
    shop = frappe.request.args.get("shop", "altomoda-njnkghxg.myshopify.com")

    if not code:
        return {"error": "No code in request", "args": dict(frappe.request.args)}

    client_id = getattr(frappe.conf, "shopify_client_id", "")
    client_secret = getattr(frappe.conf, "shopify_client_secret", "")

    post_data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }).encode()

    req = urllib.request.Request(
        "https://" + shop + "/admin/oauth/access_token",
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())

    token = result.get("access_token", "")

    if token:
        site_config_path = frappe.get_site_path("site_config.json")
        with open(site_config_path) as f:
            cfg = json.load(f)
        cfg["shopify_access_token"] = token
        cfg["shopify_domain"] = shop
        with open(site_config_path, "w") as f:
            json.dump(cfg, f, indent=1)

        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = (
            "http://localhost:8888/alaiy-os-selfserve-prototype.html"
            "?shopify_connected=1&shop=" + shop
        )

    return {"token_saved": bool(token), "shop": shop}


@frappe.whitelist()
def shopify_get_products(limit=50):
    """Pull products from Shopify via GraphQL."""
    from alaiy_os_erpnext.shopify.graphql import ShopifyGraphQL
    shopify = ShopifyGraphQL()
    return shopify.get_products(limit=int(limit))
