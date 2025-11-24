# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from havano_sync.havano_sync.utils.sync_api import SyncAPI


class HavanoSyncSettings(Document):
	def get_target_url(self):
		"""Get the target URL based on instance type"""
		if self.instance_type == "Local":
			return self.cloud_url
		elif self.instance_type == "Cloud":
			return self.local_url
		return None
	
	@frappe.whitelist()
	def test_connection(self):
		"""Test connection to the remote instance"""
		try:
			if not self.admin_api_key or not self.admin_api_secret:
				return {
					"status": "error",
					"message": "API Key and Secret must be configured"
				}
			
			target_url = self.get_target_url()
			if not target_url:
				return {
					"status": "error",
					"message": "Target URL not configured. Please set Cloud URL (for Local instance) or Local URL (for Cloud instance)."
				}
			
			api_client = SyncAPI(target_url, self.admin_api_key, self.admin_api_secret)
			if api_client.test_connection():
				return {
					"status": "success",
					"message": f"Connection successful! Connected to {target_url}",
					"target_url": target_url
				}
			else:
				return {
					"status": "error",
					"message": "Connection failed. Please check your URL, API Key, and API Secret."
				}
		
		except Exception as e:
			frappe.log_error(title="Connection Test Failed", message=frappe.get_traceback())
			error_message = str(e)
			# Extract more user-friendly error messages
			if "Connection" in error_message or "timeout" in error_message.lower():
				error_message = "Unable to reach the remote instance. Please check the URL and network connectivity."
			elif "401" in error_message or "403" in error_message or "Unauthorized" in error_message:
				error_message = "Authentication failed. Please check your API Key and API Secret."
			
			return {
				"status": "error",
				"message": f"Connection test failed: {error_message}"
			}
