# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from havano_sync.havano_sync.utils.sync_api import SyncAPI


class HavanoSyncSettings(Document):
	def get_target_url(self):
		"""Get the target URL (remote server URL)"""
		return self.remote_url
	
	@frappe.whitelist()
	def test_connection(self):
		"""Test connection to the remote instance"""
		try:
			if not self.admin_api_key or not self.admin_api_secret:
				return {
					"status": "error",
					"message": "API Key and Secret must be configured"
				}
			
			if not self.remote_url:
				return {
					"status": "error",
					"message": "Remote Server URL must be configured"
				}
			
			# Get decrypted API secret
			from havano_sync.havano_sync.tasks.sync import get_decrypted_api_secret
			api_secret = get_decrypted_api_secret(self)
			
			if not api_secret:
				return {
					"status": "error",
					"message": "API Secret could not be decrypted. Please re-enter and save the API Secret."
				}
			
			api_client = SyncAPI(self.remote_url, self.admin_api_key, api_secret)
			success, error_message = api_client.test_connection()
			
			if success:
				return {
					"status": "success",
					"message": f"Connection successful! Connected to {self.remote_url}",
					"target_url": self.remote_url
				}
			else:
				return {
					"status": "error",
					"message": error_message or "Connection failed. Please check your URL, API Key, and API Secret."
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
