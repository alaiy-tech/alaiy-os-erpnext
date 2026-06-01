"""
Shopify OAuth callback handler for alaiy OS ERPNext.
Receives the ?code= redirect, exchanges for access token, saves to site_config.json.
"""
import frappe
import json


def oauth_callback():
    """
    Handles the Shopify OAuth redirect:
    GET /api/method/alaiy_os_erpnext.shopify.oauth_callback?code=xxx&state=yyy
    Exchanges code for access token, saves to site_config.json, redirects to success page.
    """
    import urllib.request
    import urllib.parse

    code = frappe.request.args.get("code", "")
    state = frappe.request.args.get("state", "")
    shop = frappe.request.args.get("shop", "altomoda-njnkghxg.myshopify.com")

    if not code:
        frappe.respond_as_web_page(
            "Shopify OAuth Error",
            "<h3>No code received</h3><p>Query: " + str(dict(frappe.request.args)) + "</p>",
            http_status_code=400
        )
        return

    client_id = getattr(frappe.conf, "shopify_client_id", "")
    client_secret = getattr(frappe.conf, "shopify_client_secret", "")

    # Exchange code for token
    post_data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }).encode()

    req = urllib.request.Request(
        f"https://{shop}/admin/oauth/access_token",
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    try:
        with urllib.request.urlopen(req) as r:
            result = json.loads(r.read())
    except Exception as e:
        frappe.respond_as_web_page(
            "Token Exchange Failed",
            f"<h3>Error</h3><p>{e}</p>",
            http_status_code=500
        )
        return

    token = result.get("access_token", "")
    if not token:
        frappe.respond_as_web_page(
            "No Token",
            f"<h3>No token in response</h3><pre>{result}</pre>",
            http_status_code=500
        )
        return

    # Save token to site_config.json
    site_config_path = frappe.get_site_path("site_config.json")
    with open(site_config_path) as f:
        cfg = json.load(f)
    cfg["shopify_access_token"] = token
    cfg["shopify_domain"] = shop
    with open(site_config_path, "w") as f:
        json.dump(cfg, f, indent=1)

    # Log it
    frappe.logger().info(f"Shopify token saved for shop: {shop}")

    frappe.respond_as_web_page(
        "Shopify Connected",
        f"""
        <div style="font-family:sans-serif;padding:40px;text-align:center;max-width:600px;margin:auto">
        <h2 style="color:#2ecc71">Shopify connected!</h2>
        <p>Access token saved to ERPNext configuration.</p>
        <p style="color:#888;font-size:12px">Token: {token[:20]}...{token[-6:]}</p>
        <p><a href="http://localhost:8888/alaiy-os-selfserve-prototype.html">Return to alaiy OS dashboard</a></p>
        </div>
        """,
        http_status_code=200
    )
