app_name = "alaiy_os_erpnext"
app_title = "Alaiy OS ERPNext"
app_publisher = "alaiy"
app_description = "alaiy OS — ERPNext custom app: Amazon SP API + Shopify GraphQL connectors, 4 DocTypes, feed/alert engine"
app_email = "pradyun@alaiy.com"
app_license = "MIT"
app_version = "0.0.1"

scheduler_events = {
    "all": [
        "alaiy_os_erpnext.scheduler.shopify_order_sync.sync_shopify_orders"
    ],
    "15_minutes": [
        "alaiy_os_erpnext.scheduler.alert_generator.generate_alerts"
    ],
    "hourly": [
        "alaiy_os_erpnext.scheduler.competitor_pricer.update_competitor_prices"
    ],
    "daily": [
        "alaiy_os_erpnext.scheduler.health_poller.poll_amazon_health"
    ],
}

override_whitelisted_methods = {}

fixtures = [
    {"dt": "Custom DocType", "filters": [["module", "=", "Alaiy Os Erpnext"]]},
    "Marketplace Alert",
    "Account Health Snapshot",
    "Reorder Policy",
    "Competitor Listing",
]

website_route_rules = [
    {
        "from_route": "/api/alaiy_os/shopify_webhook",
        "to_route": "alaiy_os_erpnext.shopify.webhooks.handle_webhook",
    }
]

jinja = {
    "methods": [],
    "filters": [],
}
