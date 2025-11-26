# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from havano_sync.havano_sync.tasks.sync import (
	sync_all_pending_documents,
	sync_single_document,
	fetch_document_from_remote,
	fetch_all_documents_from_remote,
	get_sync_settings,
	get_decrypted_api_secret
)
from havano_sync.havano_sync.utils.sync_api import SyncAPI


@frappe.whitelist()
def trigger_sync_all(doctype: str = None):
	"""
	API endpoint to manually trigger sync for all pending documents
	Can optionally filter by doctype
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_sync_all
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_sync_all?doctype=Customer
	"""
	return sync_all_pending_documents(doctype)


@frappe.whitelist()
def trigger_sync_single(doctype: str, name: str):
	"""
	API endpoint to manually trigger sync for a single document
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_sync_single
		Body: {"doctype": "Customer", "name": "CUST-001"}
	"""
	return sync_single_document(doctype, name)


@frappe.whitelist()
def test_connection(remote_url=None, admin_api_key=None, admin_api_secret=None):
	"""
	API endpoint to test connection to remote server
	Accepts optional parameters to test with form values before saving
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.test_connection
		POST /api/method/havano_sync.havano_sync.api.sync.test_connection?remote_url=...&admin_api_key=...&admin_api_secret=...
	"""
	try:
		# Get from saved settings (password field needs to be read from database to be decrypted)
		settings = get_sync_settings()
		test_url = settings.remote_url
		test_key = settings.admin_api_key
		
		# Use the helper function to get decrypted API secret
		test_secret = get_decrypted_api_secret(settings)
		
		# If parameters are provided and form is dirty, use those (but still need to save first)
		# For password fields, we must read from saved document
		if remote_url and admin_api_key:
			# Use provided URL and key, but secret must come from saved document
			test_url = remote_url
			test_key = admin_api_key
			# Always use secret from saved document (password fields are encrypted)
		
		# Strip whitespace from credentials
		if test_key:
			test_key = test_key.strip()
		if test_secret:
			test_secret = test_secret.strip()
		if test_url:
			test_url = test_url.strip()
		
		if not test_key:
			return {
				"status": "error",
				"message": "API Key is not configured. Please enter and save the API Key."
			}
		
		if not test_secret:
			return {
				"status": "error",
				"message": "API Secret is not configured. Please enter and save the API Secret, then try again."
			}
		
		if not test_url:
			return {
				"status": "error",
				"message": "Remote Server URL must be configured"
			}
		
		# Check if password appears to be encrypted (starts with $)
		# This would indicate it wasn't decrypted properly
		if test_secret.startswith('$'):
			return {
				"status": "error",
				"message": "API Secret appears to be encrypted. Please re-enter and save the API Secret, then try again."
			}
		
		api_client = SyncAPI(test_url, test_key, test_secret)
		success, error_message = api_client.test_connection()
		
		if success:
			return {
				"status": "success",
				"message": f"Connection successful! Connected to {test_url}",
				"target_url": test_url
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


@frappe.whitelist()
def trigger_fetch_single(doctype: str, name: str):
	"""
	API endpoint to manually trigger fetch for a single document from remote
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_fetch_single
		Body: {"doctype": "Customer", "name": "CUST-001"}
	"""
	return fetch_document_from_remote(doctype, name)


@frappe.whitelist()
def trigger_fetch_all(doctype: str = None):
	"""
	API endpoint to manually trigger fetch for all documents from remote
	Can optionally filter by doctype
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_fetch_all
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_fetch_all?doctype=Customer
	"""
	return fetch_all_documents_from_remote(doctype)

