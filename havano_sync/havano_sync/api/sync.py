# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
import requests
from havano_sync.havano_sync.tasks.sync import (
	sync_all_pending_documents,
	sync_single_document,
	fetch_document_from_remote,
	fetch_all_documents_from_remote,
	get_sync_settings,
	get_decrypted_api_secret,
	fetch_item_prices_and_exchange_rates
)
from havano_sync.havano_sync.tasks.utils import fix_field_options_with_local_suffix, fix_renamed_doctypes
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
def get_remote_apps():
	"""
	API endpoint to get list of installed apps from remote server
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.get_remote_apps
	"""
	try:
		settings = get_sync_settings()
		
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return {
				"status": "error",
				"message": "Please configure Remote Server URL, Admin API Key, and Admin API Secret first."
			}
		
		api_secret = get_decrypted_api_secret(settings)
		if not api_secret:
			return {
				"status": "error",
				"message": "Could not decrypt API Secret. Please re-enter and save the API Secret."
			}
		
		# Create API client
		api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
		
		# Get installed apps from remote using frappe.utils.change_log.get_versions
		# This is the same function that show_about() uses
		try:
			# Call frappe.utils.change_log.get_versions from remote server
			endpoint = "frappe.utils.change_log.get_versions"
			versions = api_client._make_request("GET", endpoint, params={})
			
			if versions and isinstance(versions, dict):
				# Convert the versions dict to our apps list format
				apps = []
				for app_name, app_info in versions.items():
					apps.append({
						"app_name": app_name,
						"app_version": app_info.get("version", ""),
						"app_title": app_info.get("title", app_name),
						"app_description": app_info.get("description", ""),
						"branch": app_info.get("branch", ""),
						"branch_version": app_info.get("branch_version", "")
					})
				
				return {
					"status": "success",
					"apps": apps,
					"count": len(apps),
					"versions": versions  # Include full versions dict for compatibility
				}
			else:
				return {
					"status": "success",
					"apps": [],
					"count": 0,
					"message": "No apps found on remote server."
				}
		except requests.exceptions.HTTPError as e:
			if e.response and e.response.status_code == 403:
				# Permission denied - provide helpful error message
				frappe.log_error(
					title="Permission denied getting remote apps",
					message=f"403 Forbidden: API user does not have permission to access get_versions. Error: {str(e)}"
				)
				return {
					"status": "error",
					"message": "Permission denied: The API user does not have permission to access installed apps on the remote server. Please ensure the API user (associated with the Admin API Key) has the 'System Manager' role."
				}
			# For other HTTP errors, return error message
			frappe.log_error(
				title="Failed to get remote apps",
				message=f"HTTP Error getting apps: {str(e)}"
			)
			return {
				"status": "error",
				"message": f"Failed to get apps from remote server: HTTP {e.response.status_code if e.response else 'unknown'} error. Please ensure the API user has proper permissions."
			}
		except Exception as e:
			frappe.log_error(
				title="Failed to get remote apps",
				message=f"Error getting apps from remote: {str(e)}"
			)
			return {
				"status": "error",
				"message": f"Failed to get apps from remote server: {str(e)}"
			}
	except Exception as e:
		frappe.log_error(
			title="Get Remote Apps Failed",
			message=frappe.get_traceback()
		)
		return {
			"status": "error",
			"message": f"Failed to get remote apps: {str(e)}"
		}


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


@frappe.whitelist()
def get_installed_apps_info():
	"""
	API endpoint to get installed apps information (similar to show_about())
	This can be called on the remote server to get apps list
	
	Usage:
		GET /api/method/havano_sync.havano_sync.api.sync.get_installed_apps_info
	"""
	try:
		import frappe
		from frappe.utils import get_site_info
		
		# Get installed apps
		installed_apps = frappe.get_installed_apps()
		
		# Get app details from Installed Application doctype
		apps_with_versions = []
		for app_name in installed_apps:
			app_version = ""
			try:
				# Try to get version from Installed Application
				installed_app = frappe.get_doc("Installed Application", app_name)
				app_version = installed_app.app_version or ""
			except:
				# If not found, try to get from app's hooks or version file
				try:
					app_path = frappe.get_app_path(app_name)
					import os
					version_file = os.path.join(app_path, "..", "..", app_name, "version.txt")
					if os.path.exists(version_file):
						with open(version_file, "r") as f:
							app_version = f.read().strip()
				except:
					pass
			
			apps_with_versions.append({
				"app_name": app_name,
				"app_version": app_version
			})
		
		# Get site information
		site_info = get_site_info()
		
		return {
			"apps": apps_with_versions,
			"site_info": site_info
		}
	except Exception as e:
		frappe.log_error(
			title="Failed to get installed apps info",
			message=frappe.get_traceback()
		)
		return {
			"apps": [],
			"site_info": {},
			"error": str(e)
		}


@frappe.whitelist()
def fix_field_options_local_suffix():
	"""
	API endpoint to fix field options that incorrectly reference doctypes with -Local suffix
	This fixes database corruption where field options have doctype names with -Local suffix
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.fix_field_options_local_suffix
	"""
	return fix_field_options_with_local_suffix()


@frappe.whitelist()
def fix_renamed_doctypes_job():
	"""
	API endpoint to fix DocType definitions that were incorrectly renamed with -Local suffix
	This fixes database corruption where DocType definitions have -Local suffix
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.fix_renamed_doctypes_job
	"""
	return fix_renamed_doctypes()


@frappe.whitelist()
def find_document_by_sync_reference(doctype: str, sync_reference: str, sync_type: str = "Local"):
	"""
	API endpoint to find a document by sync_reference field.
	This is used by the remote server to help local server find documents.
	
	Args:
		doctype: Document type to search
		sync_reference: The sync_reference value to search for
		sync_type: The sync_type value (default: "Local")
	
	Returns:
		Document name if found, None otherwise
	"""
	try:
		# Use frappe.get_all to find document by sync_reference
		# This works locally but not via frappe.client.get_list (which has restrictions)
		docs = frappe.get_all(
			doctype,
			filters={
				"sync_reference": sync_reference,
				"sync_type": sync_type
			},
			limit=1,
			fields=["name"]
		)
		
		if docs and len(docs) > 0:
			return docs[0].name
		return None
	except Exception as e:
		frappe.logger().error(f"Error finding document by sync_reference: {str(e)}")
		return None

@frappe.whitelist()
def trigger_fetch_item_prices_and_exchange_rates():
	"""
	API endpoint to manually trigger fetch of item prices and exchange rates
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_fetch_item_prices_and_exchange_rates
	"""
	return fetch_item_prices_and_exchange_rates()