"""
Amazon SP API wrapper for alaiy OS ERPNext.
Uses python-amazon-sp-api (pip install python-amazon-sp-api).
Credentials are read from site_config.json (frappe.conf).
"""
import frappe
import json
from datetime import datetime, timedelta


def get_sp_client():
    """Instantiate AlaiyAmazonSP from site_config.json credentials."""
    conf = frappe.conf
    return AlaiyAmazonSP(
        refresh_token=conf.sp_api_refresh_token,
        lwa_client_id=conf.sp_api_client_id,
        lwa_client_secret=conf.sp_api_client_secret,
        seller_id=getattr(conf, "sp_api_seller_id", ""),
        marketplace_id=getattr(conf, "sp_api_marketplace_id", "A21TJRUUN4KGV"),
        region=getattr(conf, "sp_api_region", "eu"),
    )


def get_seller_id():
    return getattr(frappe.conf, "sp_api_seller_id", "")


def get_marketplace_id():
    return getattr(frappe.conf, "sp_api_marketplace_id", "A21TJRUUN4KGV")


class AlaiyAmazonSP:
    """
    Thin wrapper around python-amazon-sp-api that exposes only the operations
    alaiy OS needs. Each method returns plain Python dicts/lists suitable for
    JSON serialisation and storage in Frappe.
    """

    def __init__(self, refresh_token, lwa_client_id, lwa_client_secret,
                 seller_id="", marketplace_id="A21TJRUUN4KGV", region="eu"):
        self.seller_id = seller_id
        self.marketplace_id = marketplace_id
        self.region = region
        self.credentials = {
            "refresh_token": refresh_token,
            "lwa_app_id": lwa_client_id,
            "lwa_client_secret": lwa_client_secret,
        }

    def _marketplace_enum(self):
        from sp_api.base import Marketplaces
        marketplace_map = {
            "A21TJRUUN4KGV": Marketplaces.IN,
            "ATVPDKIKX0DER": Marketplaces.US,
            "A1F83G8C2ARO7P": Marketplaces.UK,
            "A1PA6795UKMFR9": Marketplaces.DE,
            "APJ6JRA9NG5V4":  Marketplaces.IT,
        }
        return marketplace_map.get(self.marketplace_id, Marketplaces.IN)

    def _orders_api(self):
        from sp_api.api import Orders
        return Orders(credentials=self.credentials, marketplace=self._marketplace_enum())

    def _listings_api(self):
        from sp_api.api import ListingsItems
        return ListingsItems(credentials=self.credentials, marketplace=self._marketplace_enum())

    def _product_pricing_api(self):
        from sp_api.api import ProductPricing
        return ProductPricing(credentials=self.credentials, marketplace=self._marketplace_enum())

    def _feeds_api(self):
        from sp_api.api import Feeds
        return Feeds(credentials=self.credentials, marketplace=self._marketplace_enum())

    # ── Orders ────────────────────────────────────────────────────────────────

    def get_orders(self, days_ago=1):
        """Return list of order dicts created in the last `days_ago` days."""
        created_after = (datetime.utcnow() - timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        api = self._orders_api()
        response = api.get_orders(
            CreatedAfter=created_after,
            MarketplaceIds=[self.marketplace_id],
        )
        orders = response.payload.get("Orders", [])
        result = []
        for o in orders:
            result.append({
                "amazon_order_id": o.get("AmazonOrderId"),
                "purchase_date": o.get("PurchaseDate"),
                "status": o.get("OrderStatus"),
                "fulfillment_channel": o.get("FulfillmentChannel"),
                "buyer_email": o.get("BuyerInfo", {}).get("BuyerEmail", ""),
                "order_total": o.get("OrderTotal", {}).get("Amount", 0),
                "currency": o.get("OrderTotal", {}).get("CurrencyCode", "INR"),
                "number_of_items": o.get("NumberOfItemsShipped", 0) + o.get("NumberOfItemsUnshipped", 0),
                "ship_service_level": o.get("ShipServiceLevel", ""),
            })
        return result

    def get_order_items(self, amazon_order_id):
        """Return list of item dicts for a given Amazon order ID."""
        api = self._orders_api()
        response = api.get_order_items(orderId=amazon_order_id)
        items = response.payload.get("OrderItems", [])
        return [
            {
                "asin": i.get("ASIN"),
                "seller_sku": i.get("SellerSKU"),
                "title": i.get("Title"),
                "qty_ordered": i.get("QuantityOrdered", 0),
                "qty_shipped": i.get("QuantityShipped", 0),
                "item_price": i.get("ItemPrice", {}).get("Amount", 0),
                "currency": i.get("ItemPrice", {}).get("CurrencyCode", "INR"),
            }
            for i in items
        ]

    # ── Account Health ────────────────────────────────────────────────────────

    def get_account_health(self):
        """
        Return account health metrics dict.
        Amazon SP API v2 does not expose account health scores via a public endpoint.
        We verify the connection using get_marketplace_participation and return
        marketplace status. Full health metrics (ODR, etc.) require Seller Central UI
        or DataKiosk/Reports API integration (coming in Phase 2).
        """
        try:
            from sp_api.api import Sellers
            api = Sellers(credentials=self.credentials)
            response = api.get_marketplace_participation()
            payload = response.payload or {}
            participations = payload.get("payload", payload) if isinstance(payload, dict) else payload
            # Build a minimal health snapshot showing marketplace status
            metrics = {
                "connected": True,
                "seller_id": self.seller_id,
                "marketplace_id": self.marketplace_id,
            }
            # Extract marketplace status if available
            if isinstance(participations, list):
                for p in participations:
                    mkt = p.get("marketplace", {})
                    if mkt.get("id") == self.marketplace_id:
                        metrics["marketplace_name"] = mkt.get("name", "Amazon India")
                        metrics["marketplace_country"] = mkt.get("countryCode", "IN")
                        part = p.get("participation", {})
                        metrics["is_participating"] = part.get("isParticipating", True)
                        break
            return {"metrics": metrics, "note": "Full health scores (ODR, LSR) available via Seller Central or DataKiosk API."}
        except Exception as e:
            frappe.log_error(f"SP API health check error: {e}", "SP API")
            return {"metrics": {}, "connected": False, "error": str(e)}

    # ── Competitive Pricing ───────────────────────────────────────────────────

    def get_competitive_pricing(self, asin_list):
        """Return dict of ASIN -> {lowest_price, buybox_price}."""
        api = self._product_pricing_api()
        result = {}
        for i in range(0, len(asin_list), 20):
            batch = asin_list[i:i + 20]
            try:
                response = api.get_competitive_pricing_for_asins(
                    MarketplaceId=self.marketplace_id, Asins=batch
                )
                for item in response.payload:
                    asin = item.get("ASIN")
                    pricing = item.get("Product", {}).get("CompetitivePricing", {})
                    prices = pricing.get("CompetitivePrices", [])
                    lowest = None
                    buybox = None
                    for p in prices:
                        amount = p.get("Price", {}).get("ListingPrice", {}).get("Amount", 0)
                        if p.get("condition") == "New":
                            if lowest is None or amount < lowest:
                                lowest = amount
                        if p.get("belongsToRequester"):
                            buybox = amount
                    result[asin] = {
                        "lowest_price": lowest or 0,
                        "buybox_price": buybox or 0,
                    }
            except Exception as e:
                frappe.log_error(f"Competitive pricing error for batch {batch}: {e}", "SP API")
        return result

    # ── Listings / Repricing ──────────────────────────────────────────────────

    def reprice_listing(self, sku, price):
        """Patch a listings item price via patchListingsItem."""
        api = self._listings_api()
        patches = [
            {
                "op": "replace",
                "path": "/attributes/purchasable_offer",
                "value": [
                    {
                        "marketplace_id": self.marketplace_id,
                        "currency": "INR",
                        "our_price": [{"schedule": [{"value_with_tax": price}]}],
                    }
                ],
            }
        ]
        try:
            response = api.patch_listings_item(
                sellerId=self.seller_id,
                sku=sku,
                marketplaceIds=[self.marketplace_id],
                body={"productType": "PRODUCT", "patches": patches},
            )
            return {"status": response.payload.get("status"), "sku": sku, "price": price}
        except Exception as e:
            frappe.log_error(f"Reprice error for SKU {sku}: {e}", "SP API")
            return {"error": str(e), "sku": sku}

    def get_listings_item(self, sku):
        """Get full listing details for a SKU."""
        api = self._listings_api()
        try:
            response = api.get_listings_item(
                sellerId=self.seller_id,
                sku=sku,
                marketplaceIds=[self.marketplace_id],
                includedData=["summaries", "attributes", "issues", "offers"],
            )
            return response.payload
        except Exception as e:
            frappe.log_error(f"Get listing error for SKU {sku}: {e}", "SP API")
            return {"error": str(e)}

    # ── Inventory ─────────────────────────────────────────────────────────────

    def update_inventory_quantity(self, sku, qty):
        """Update FBM inventory quantity via Feeds API."""
        api = self._feeds_api()
        feed_content = "sku\tquantity\n" + f"{sku}\t{int(qty)}\n"
        try:
            import requests as req
            doc_response = api.create_feed_document(contentType="text/tab-separated-values;charset=UTF-8")
            doc = doc_response.payload
            feed_doc_id = doc["feedDocumentId"]
            upload_url = doc["url"]
            req.put(upload_url, data=feed_content.encode("utf-8"),
                    headers={"Content-Type": "text/tab-separated-values;charset=UTF-8"})
            feed_response = api.create_feed(body={
                "feedType": "POST_INVENTORY_AVAILABILITY_DATA",
                "marketplaceIds": [self.marketplace_id],
                "inputFeedDocumentId": feed_doc_id,
            })
            return {"feed_id": feed_response.payload.get("feedId"), "sku": sku, "qty": qty}
        except Exception as e:
            frappe.log_error(f"Inventory update error for SKU {sku}: {e}", "SP API")
            return {"error": str(e)}

    # ── Shipment Confirmation ─────────────────────────────────────────────────

    def confirm_shipment(self, amazon_order_id, carrier, tracking_number, ship_date=None):
        """Confirm shipment for an MFN order."""
        if ship_date is None:
            ship_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        api = self._orders_api()
        try:
            payload = {
                "packageDetail": {
                    "packageReferenceId": "1",
                    "carrierCode": carrier,
                    "trackingNumber": tracking_number,
                    "shipDate": ship_date,
                    "orderItems": [],
                }
            }
            response = api.confirm_shipment(orderId=amazon_order_id, payload=payload)
            return {"status": "confirmed", "amazon_order_id": amazon_order_id, "awb": tracking_number}
        except Exception as e:
            frappe.log_error(f"Confirm shipment error for {amazon_order_id}: {e}", "SP API")
            return {"error": str(e)}
