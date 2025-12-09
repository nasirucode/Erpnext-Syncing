# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from havano_sync.havano_sync.utils.sync_api import SyncAPI


def update_naming_series_options_locally(doctype: str, naming_series_name: str) -> bool:
	"""
	Update the naming series options in a DocType locally.
	
	Args:
		doctype: Document type (e.g., "Payment Entry", "Sales Invoice")
		naming_series_name: Name of the naming series to add
	
	Returns:
		True if naming series was added or already exists, False otherwise
	"""
	if not naming_series_name:
		return True
	
	try:
		# Get the DocType document
		doctype_doc = frappe.get_doc("DocType", doctype)
		
		# Find the naming_series field
		naming_series_field = None
		for field in doctype_doc.fields:
			if field.fieldname == 'naming_series':
				naming_series_field = field
				break
		
		if not naming_series_field:
			# No naming_series field, nothing to do
			return True
		
		# Get current options
		options = naming_series_field.options or ''
		
		# Check if naming series is already in options
		options_list = [opt.strip() for opt in options.split('\n') if opt.strip()]
		already_exists = naming_series_name in options_list
		
		if not already_exists:
			# Add naming series to options
			options_list.append(naming_series_name)
			naming_series_field.options = '\n'.join(options_list)
		
		# Set as default (whether it was just added or already existed)
		naming_series_field.default = naming_series_name
		
		# Save the DocType
		doctype_doc.save(ignore_permissions=True)
		frappe.db.commit()
		
		# Clear cache to ensure UI reflects the changes
		frappe.clear_cache(doctype=doctype)
		frappe.reload_doctype(doctype, force=True)
		
		return True
		
	except Exception as e:
		frappe.log_error(
			title="Failed to update naming series options locally",
			message=f"Could not add naming series {naming_series_name} to options for {doctype}: {str(e)}"
		)
		return False


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
	
	