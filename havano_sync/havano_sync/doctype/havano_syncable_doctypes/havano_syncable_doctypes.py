# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class HavanoSyncableDoctypes(Document):
	def validate(self):
		"""
		Validate and clean doctype name - remove -Local suffix if present
		Doctype names should never have -Local suffix in syncable doctypes
		"""
		if self.doctypes and self.doctypes.endswith("-Local"):
			# Remove -Local suffix (6 characters)
			original_doctype = self.doctypes
			self.doctypes = self.doctypes[:-6]
			frappe.logger().warning(
				f"Removed -Local suffix from doctype name in syncable doctypes: "
				f"{original_doctype} -> {self.doctypes}"
			)
