# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from datetime import datetime, timedelta


class HavanoSyncQueue(Document):
	def before_insert(self):
		if not self.status:
			self.status = "Queued"
		if not self.queued_at:
			self.queued_at = frappe.utils.now_datetime()
		if not self.retry_count:
			self.retry_count = 0
	
	def calculate_next_retry(self):
		"""Calculate next retry time with exponential backoff"""
		# Exponential backoff: 1min, 5min, 15min, 30min, 1hr
		backoff_minutes = [1, 5, 15, 30, 60]
		retry_index = min(self.retry_count, len(backoff_minutes) - 1)
		minutes = backoff_minutes[retry_index]
		return frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=minutes)

