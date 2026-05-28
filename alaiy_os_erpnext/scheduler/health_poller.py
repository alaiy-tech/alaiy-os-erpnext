import frappe


def poll_amazon_health():
    """Daily: poll Amazon SP API for account health and save snapshot."""
    try:
        from alaiy_os_erpnext.amazon.sp_api import get_sp_client
        sp = get_sp_client()
        health = sp.get_account_health()

        snapshot = frappe.new_doc("Account Health Snapshot")
        snapshot.marketplace = "amazon_in"
        snapshot.connector = "amazon_sp_api"
        snapshot.snapshot_date = frappe.utils.today()

        metrics = health.get("metrics", {})

        snapshot.metric_1_name = "Order Defect Rate"
        snapshot.metric_1_value = metrics.get("odr", 0)
        snapshot.metric_1_limit = 1.0
        snapshot.metric_1_status = "fire" if metrics.get("odr", 0) >= 0.7 else "ok"

        snapshot.metric_2_name = "Late Shipment Rate"
        snapshot.metric_2_value = metrics.get("late_shipment_rate", 0)
        snapshot.metric_2_limit = 4.0
        snapshot.metric_2_status = "warn" if metrics.get("late_shipment_rate", 0) >= 2.8 else "ok"

        snapshot.metric_3_name = "Pre-fulfillment Cancel Rate"
        snapshot.metric_3_value = metrics.get("cancel_rate", 0)
        snapshot.metric_3_limit = 2.5
        snapshot.metric_3_status = "warn" if metrics.get("cancel_rate", 0) >= 1.75 else "ok"

        snapshot.metric_4_name = "Valid Tracking Rate"
        snapshot.metric_4_value = metrics.get("valid_tracking_rate", 100)
        snapshot.metric_4_limit = 95.0
        snapshot.metric_4_status = "warn" if metrics.get("valid_tracking_rate", 100) < 96 else "ok"

        snapshot.open_claims = metrics.get("a_to_z_claims", 0)
        snapshot.raw_response = frappe.as_json(health)
        snapshot.flags.ignore_mandatory = True
        snapshot.insert(ignore_permissions=True)
        frappe.db.commit()

        if snapshot.metric_1_status == "fire":
            _create_health_alert("odr", snapshot)

    except Exception as e:
        frappe.log_error(f"Amazon health poll failed: {e}", "Health Poller")


def _create_health_alert(metric, snapshot):
    existing = frappe.db.exists("Marketplace Alert", {
        "alert_type": "Account Health",
        "marketplace": "amazon_in",
        "status": ["in", ["Open", "Snoozed"]],
    })
    if not existing:
        alert = frappe.new_doc("Marketplace Alert")
        alert.alert_type = "Account Health"
        alert.severity = "fire"
        alert.marketplace = "amazon_in"
        alert.title = "ODR trending to Amazon limit"
        alert.description = (
            f"Current ODR: {snapshot.metric_1_value}% "
            f"(limit: {snapshot.metric_1_limit}%). "
            f"Open A-to-Z claims: {snapshot.open_claims}"
        )
        alert.status = "Open"
        alert.flags.ignore_mandatory = True
        alert.insert(ignore_permissions=True)
        frappe.db.commit()
