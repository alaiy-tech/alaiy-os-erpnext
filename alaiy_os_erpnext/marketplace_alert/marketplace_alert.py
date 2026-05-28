import frappe
from frappe.model.document import Document


class MarketplaceAlert(Document):

    def before_insert(self):
        if not self.status:
            self.status = "Open"

    def validate(self):
        if self.status != "Snoozed":
            self.snoozed_until = None
