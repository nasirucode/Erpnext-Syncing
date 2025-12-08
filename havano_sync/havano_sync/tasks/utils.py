# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
from datetime import date, datetime, timedelta
from frappe.utils import cint
from typing import Optional, Dict, Any, List
from havano_sync.havano_sync.utils.sync_api import SyncAPI


def serialize_document_data(data: Dict[str, Any]) -> str:
	"""
	Serialize document data to JSON, handling date/datetime/timedelta objects
	"""
	def json_serializer(obj):
		"""JSON serializer for objects not serializable by default json code"""
		if isinstance(obj, (date, datetime)):
			return obj.isoformat()
		elif isinstance(obj, timedelta):
			# Convert timedelta to HH:MM:SS format (Frappe time format)
			total_seconds = int(obj.total_seconds())
			hours = total_seconds // 3600
			minutes = (total_seconds % 3600) // 60
			seconds = total_seconds % 60
			return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
		raise TypeError(f"Type {type(obj)} not serializable")
	
	return json.dumps(data, default=json_serializer)


def get_sync_settings():
	"""Get Havano Sync Settings"""
	try:
		return frappe.get_single("Havano Sync Settings")
	except:
		frappe.throw("Havano Sync Settings not found. Please configure it first.")


def is_submittable_doctype(doctype: str) -> bool:
	"""
	Check if a doctype is submittable (has docstatus field)
	
	Args:
		doctype: Document type to check
	
	Returns:
		True if doctype is submittable, False otherwise
	"""
	try:
		meta = frappe.get_meta(doctype)
		return meta.is_submittable
	except Exception:
		# If we can't get meta, assume it's not submittable
		return False


def belongs_to_company(doc_data: Dict[str, Any], doctype: str, company: Optional[str]) -> bool:
	"""
	Check if a document belongs to the specified company
	
	Args:
		doc_data: Document data dictionary
		doctype: Document type
		company: Company name to check against (None means no filter)
	
	Returns:
		True if document belongs to company or company is not specified, False otherwise
	"""
	if not company:
		return True
	
	# Direct company field
	if 'company' in doc_data and doc_data.get('company') == company:
		return True
	
	# For doctypes that have company field, check it
	company_doctypes = {
		'Account', 'Warehouse', 'Cost Center', 'Sales Invoice', 'Purchase Invoice',
		'Sales Order', 'Purchase Order', 'Payment Entry', 'Journal Entry',
		'Stock Entry', 'Delivery Note', 'Purchase Receipt', 'Quotation',
		'Purchase Request', 'Material Request', 'Work Order', 'Job Card',
		'Timesheet', 'Expense Claim', 'Leave Application', 'Salary Slip',
		'Asset', 'Asset Movement', 'Landed Cost Voucher', 'Stock Reconciliation',
		'Stock Ledger Entry', 'GL Entry', 'Budget', 'Budget Account',
		'Project', 'Task', 'Issue', 'Opportunity', 'Lead', 'Customer',
		'Supplier', 'Employee', 'Employee Advance', 'Employee Loan',
		'Payroll Entry', 'Salary Structure', 'Salary Structure Assignment'
	}
	
	if doctype in company_doctypes:
		doc_company = doc_data.get('company')
		if doc_company and doc_company != company:
			return False
	
	# For Company doctype itself, check if it's the specified company
	if doctype == 'Company':
		return doc_data.get('name') == company or doc_data.get('company_name') == company
	
	return True


def get_decrypted_api_secret(settings=None):
	"""
	Get decrypted admin_api_secret from Havano Sync Settings
	For single doctypes, password fields need explicit decryption
	"""
	if settings is None:
		settings = get_sync_settings()
	
	try:
		from frappe.utils.password import get_decrypted_password
		# Get the actual document name from the database
		doc_name = frappe.db.get_value("Havano Sync Settings", None, "name")
		if not doc_name:
			# If no document exists, use doctype name
			doc_name = "Havano Sync Settings"
		return get_decrypted_password("Havano Sync Settings", doc_name, "admin_api_secret")
	except Exception as e:
		# Fallback: try to get from settings object (might be auto-decrypted)
		secret = getattr(settings, 'admin_api_secret', None)
		# If still encrypted (starts with $) or empty, try direct database access
		if not secret or (isinstance(secret, str) and secret.startswith('$')):
			try:
				# Try to get encrypted password from database and decrypt it
				encrypted_secret = frappe.db.get_value("Havano Sync Settings", None, "admin_api_secret")
				if encrypted_secret and encrypted_secret.startswith('$'):
					doc_name = frappe.db.get_value("Havano Sync Settings", None, "name") or "Havano Sync Settings"
					return get_decrypted_password("Havano Sync Settings", doc_name, "admin_api_secret")
				else:
					return encrypted_secret
			except Exception as e2:
				frappe.log_error(f"Failed to decrypt admin_api_secret: {str(e2)}", "Password Decryption Issue")
				return None
		else:
			# Secret is already decrypted or not encrypted
			return secret


def get_target_url(settings) -> Optional[str]:
	"""Get the target URL (remote server URL)"""
	return settings.remote_url


def get_syncable_doctypes(settings) -> List[Dict[str, Any]]:
	"""Get list of syncable doctypes from settings"""
	return settings.get("syncable_doctypes", [])


def should_sync_doctype(doctype: str, settings, direction: str = "send") -> bool:
	"""
	Check if a doctype should be synced
	direction: "send" (to remote) or "fetch" (from remote)
	"""
	syncable_doctypes = get_syncable_doctypes(settings)
	
	for syncable in syncable_doctypes:
		# syncable is a child table row (document object)
		syncable_doctype = syncable.doctypes
		# Remove -Local suffix if present (doctype names should never have -Local suffix)
		if syncable_doctype and syncable_doctype.endswith("-Local"):
			syncable_doctype = syncable_doctype[:-6]  # Remove "-Local" (6 characters)
			frappe.logger().warning(f"Found doctype name with -Local suffix in syncable doctypes: {syncable.doctypes}. Using {syncable_doctype} instead.")
		if syncable_doctype == doctype:
			if direction == "send":
				return cint(syncable.get('send', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'send', 0))
			elif direction == "fetch":
				return cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			# For backward compatibility, check if either is enabled
			send = cint(syncable.get('send', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'send', 0))
			fetch = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			return send or fetch
	return False


def check_internet_connection(settings) -> bool:
	"""
	Check if internet connection is available by testing connection to remote server
	Returns True if internet is available (even if authentication fails with 401)
	Returns False only if there's no network connectivity (connection error, timeout, etc.)
	"""
	try:
		if not settings.remote_url or not settings.admin_api_key:
			return False
		
		# Get decrypted API secret
		api_secret = get_decrypted_api_secret(settings)
		if not api_secret:
			return False
		
		api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
		success, error_message = api_client.test_connection()
		
		# If we get a 401, it means internet is working but auth failed
		# We should still return True to allow syncing (auth will be checked separately)
		if not success and error_message:
			if "401" in error_message or "Authentication failed" in error_message:
				# Internet is available, just auth failed
				return True
			# Other errors likely mean no internet
			return False
		
		return success
	except Exception as e:
		# Any exception means no internet
		frappe.log_error(f"Internet connection check failed: {str(e)}")
		return False

