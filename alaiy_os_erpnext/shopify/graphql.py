"""
Shopify Admin GraphQL API client for alaiy OS ERPNext.
Credentials read from site_config.json (frappe.conf).

API version: 2025-01
Rate limiting: bucket-based (cost tracking) with exponential backoff.
"""
import time
import frappe
import requests


class ShopifyGraphQL:

    API_VERSION = "2025-01"

    def __init__(self):
        conf = frappe.conf
        shop = getattr(conf, "shopify_domain", "")
        shop = shop.replace("https://", "").replace("http://", "").rstrip("/")
        if not shop.endswith(".myshopify.com"):
            shop = f"{shop}.myshopify.com"
        self.endpoint = f"https://{shop}/admin/api/{self.API_VERSION}/graphql.json"
        self.token = getattr(conf, "shopify_access_token", "")
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": self.token,
            "Content-Type": "application/json",
        })

    def query(self, gql_string, variables=None, max_retries=5):
        """Execute a GraphQL query/mutation with cost-based throttle backoff."""
        payload = {"query": gql_string}
        if variables:
            payload["variables"] = variables

        for attempt in range(max_retries):
            resp = self.session.post(self.endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            throttle = (
                data.get("extensions", {})
                    .get("cost", {})
                    .get("throttleStatus", {})
            )
            available = throttle.get("currentlyAvailable", 9999)
            restore_rate = throttle.get("restoreRate", 100)

            if data.get("errors"):
                for err in data["errors"]:
                    if err.get("extensions", {}).get("code") == "THROTTLED":
                        wait = max(1, (100 - available) / max(restore_rate, 1))
                        wait = min(wait * (2 ** attempt), 30)
                        time.sleep(wait)
                        break
                else:
                    raise Exception(f"Shopify GraphQL errors: {data['errors']}")
                continue

            if available < 100:
                time.sleep(max(0.2, (100 - available) / max(restore_rate, 1)))

            return data.get("data", {})

        raise Exception("Max retries exceeded for Shopify GraphQL request")

    def get_orders(self, limit=50, status="open", since=None):
        """Return list of order dicts."""
        query_filter = f"status:{status}"
        if since:
            if not isinstance(since, str):
                since = since.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(since, "strftime") else str(since)
            query_filter += f' created_at:>"{since}"'

        gql = """
        query GetOrders($limit: Int!, $query: String!) {
          orders(first: $limit, query: $query, sortKey: CREATED_AT) {
            nodes {
              id
              name
              createdAt
              displayFulfillmentStatus
              displayFinancialStatus
              customer { displayName email phone }
              lineItems(first: 50) {
                nodes {
                  id title quantity sku
                  originalUnitPriceSet {
                    shopMoney { amount currencyCode }
                  }
                }
              }
              shippingAddress { address1 city province country zip }
              totalPriceSet { shopMoney { amount currencyCode } }
            }
          }
        }
        """
        data = self.query(gql, {"limit": limit, "query": query_filter})
        return data.get("orders", {}).get("nodes", [])

    def fulfill_order(self, order_id, carrier, tracking_number, notify_customer=True):
        """Create a fulfillment for an order using fulfillmentCreate mutation."""
        gql_get_fo = """
        query GetFulfillmentOrders($orderId: ID!) {
          order(id: $orderId) {
            fulfillmentOrders(first: 5) {
              nodes {
                id status
                lineItems(first: 50) {
                  nodes { id remainingQuantity }
                }
              }
            }
          }
        }
        """
        data = self.query(gql_get_fo, {"orderId": order_id})
        fo_nodes = data.get("order", {}).get("fulfillmentOrders", {}).get("nodes", [])
        open_fos = [n for n in fo_nodes if n.get("status") in ("OPEN", "IN_PROGRESS")]

        if not open_fos:
            return {"error": "No open fulfillment orders found", "order_id": order_id}

        fo = open_fos[0]
        line_items_to_fulfill = [
            {"id": li["id"], "quantity": li["remainingQuantity"]}
            for li in fo["lineItems"]["nodes"]
            if li["remainingQuantity"] > 0
        ]

        gql_fulfill = """
        mutation FulfillmentCreate($fulfillment: FulfillmentInput!) {
          fulfillmentCreate(fulfillment: $fulfillment) {
            fulfillment {
              id status
              trackingInfo { number company }
            }
            userErrors { field message }
          }
        }
        """
        variables = {
            "fulfillment": {
                "notifyCustomer": notify_customer,
                "trackingInfo": {"company": carrier, "number": tracking_number},
                "lineItemsByFulfillmentOrder": [
                    {
                        "fulfillmentOrderId": fo["id"],
                        "fulfillmentOrderLineItems": line_items_to_fulfill,
                    }
                ],
            }
        }
        result = self.query(gql_fulfill, variables)
        fc = result.get("fulfillmentCreate", {})
        errors = fc.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify fulfill errors: {errors}")
        return fc.get("fulfillment", {})

    def update_tracking(self, fulfillment_id, tracking_number, notify_customer=True):
        """Update tracking info on an existing fulfillment."""
        gql = """
        mutation FulfillmentTrackingInfoUpdate(
          $fulfillmentId: ID!
          $trackingInfoInput: FulfillmentTrackingInput!
          $notifyCustomer: Boolean
        ) {
          fulfillmentTrackingInfoUpdate(
            fulfillmentId: $fulfillmentId
            trackingInfoInput: $trackingInfoInput
            notifyCustomer: $notifyCustomer
          ) {
            fulfillment { id trackingInfo { number company } }
            userErrors { field message }
          }
        }
        """
        variables = {
            "fulfillmentId": fulfillment_id,
            "trackingInfoInput": {"number": tracking_number},
            "notifyCustomer": notify_customer,
        }
        result = self.query(gql, variables)
        fc = result.get("fulfillmentTrackingInfoUpdate", {})
        errors = fc.get("userErrors", [])
        if errors:
            raise Exception(f"Tracking update errors: {errors}")
        return fc.get("fulfillment", {})

    def cancel_order(self, order_id, reason="OTHER", notify_customer=True):
        """Cancel an order."""
        gql = """
        mutation OrderCancel(
          $orderId: ID!
          $reason: OrderCancelReason!
          $notifyCustomer: Boolean
          $refund: Boolean!
          $restock: Boolean!
        ) {
          orderCancel(
            orderId: $orderId
            reason: $reason
            notifyCustomer: $notifyCustomer
            refund: $refund
            restock: $restock
          ) {
            orderCancelUserErrors { field message }
            job { id }
          }
        }
        """
        variables = {
            "orderId": order_id,
            "reason": reason,
            "notifyCustomer": notify_customer,
            "refund": True,
            "restock": True,
        }
        result = self.query(gql, variables)
        oc = result.get("orderCancel", {})
        errors = oc.get("orderCancelUserErrors", [])
        if errors:
            raise Exception(f"Order cancel errors: {errors}")
        return {"status": "cancel_requested", "job": oc.get("job", {})}

    def set_inventory(self, inventory_item_id, location_id, quantity):
        """Set absolute inventory quantity."""
        gql = """
        mutation InventorySetQuantities($input: InventorySetQuantitiesInput!) {
          inventorySetQuantities(input: $input) {
            inventoryAdjustmentGroup {
              id
              changes { name delta }
            }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "name": "available",
                "reason": "correction",
                "quantities": [
                    {
                        "inventoryItemId": inventory_item_id,
                        "locationId": location_id,
                        "quantity": quantity,
                    }
                ],
            }
        }
        result = self.query(gql, variables)
        isq = result.get("inventorySetQuantities", {})
        errors = isq.get("userErrors", [])
        if errors:
            raise Exception(f"Inventory set errors: {errors}")
        return isq.get("inventoryAdjustmentGroup", {})

    def update_variant_price(self, product_id, variant_id, price, compare_at_price=None):
        """Update price on a product variant."""
        gql = """
        mutation ProductVariantsBulkUpdate(
          $productId: ID!
          $variants: [ProductVariantsBulkInput!]!
        ) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            product { id }
            productVariants { id price compareAtPrice }
            userErrors { field message }
          }
        }
        """
        variant_input = {"id": variant_id, "price": price}
        if compare_at_price is not None:
            variant_input["compareAtPrice"] = compare_at_price

        variables = {"productId": product_id, "variants": [variant_input]}
        result = self.query(gql, variables)
        pvbu = result.get("productVariantsBulkUpdate", {})
        errors = pvbu.get("userErrors", [])
        if errors:
            raise Exception(f"Variant price update errors: {errors}")
        variants = pvbu.get("productVariants", [])
        return variants[0] if variants else {}

    def approve_return(self, return_id):
        """Approve a return request."""
        gql = """
        mutation ReturnApproveRequest($input: ReturnApproveRequestInput!) {
          returnApproveRequest(input: $input) {
            return { id status }
            userErrors { field message }
          }
        }
        """
        result = self.query(gql, {"input": {"id": return_id}})
        rar = result.get("returnApproveRequest", {})
        errors = rar.get("userErrors", [])
        if errors:
            raise Exception(f"Return approve errors: {errors}")
        return rar.get("return", {})

    def refund_return(self, return_id, line_items=None):
        """Issue a refund for a return."""
        gql = """
        mutation ReturnRefund($returnRefundInput: ReturnRefundInput!) {
          returnRefund(returnRefundInput: $returnRefundInput) {
            refund {
              id
              totalRefundedSet { shopMoney { amount currencyCode } }
            }
            userErrors { field message }
          }
        }
        """
        refund_input = {"returnId": return_id}
        if line_items:
            refund_input["returnRefundLineItems"] = line_items

        result = self.query(gql, {"returnRefundInput": refund_input})
        rr = result.get("returnRefund", {})
        errors = rr.get("userErrors", [])
        if errors:
            raise Exception(f"Return refund errors: {errors}")
        return rr.get("refund", {})

    def get_products(self, limit=50):
        """Return list of products."""
        gql = """
        query GetProducts($limit: Int!) {
          products(first: $limit) {
            nodes {
              id title vendor status totalInventory
              variants(first: 10) {
                nodes { id sku price compareAtPrice inventoryQuantity }
              }
            }
          }
        }
        """
        data = self.query(gql, {"limit": limit})
        return data.get("products", {}).get("nodes", [])

    def create_product(self, title, description="", vendor="", tags=None, status="ACTIVE"):
        """Create a new product."""
        gql = """
        mutation ProductCreate($input: ProductInput!) {
          productCreate(input: $input) {
            product { id title status }
            userErrors { field message }
          }
        }
        """
        product_input = {
            "title": title,
            "bodyHtml": description,
            "vendor": vendor,
            "status": status,
        }
        if tags:
            product_input["tags"] = tags if isinstance(tags, list) else [tags]

        result = self.query(gql, {"input": product_input})
        pc = result.get("productCreate", {})
        errors = pc.get("userErrors", [])
        if errors:
            raise Exception(f"Product create errors: {errors}")
        return pc.get("product", {})

    def update_product(self, product_id, fields):
        """Update an existing product."""
        gql = """
        mutation ProductUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id title updatedAt }
            userErrors { field message }
          }
        }
        """
        fields["id"] = product_id
        result = self.query(gql, {"input": fields})
        pu = result.get("productUpdate", {})
        errors = pu.get("userErrors", [])
        if errors:
            raise Exception(f"Product update errors: {errors}")
        return pu.get("product", {})
