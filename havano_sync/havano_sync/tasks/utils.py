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


def is_send_only_doctype(doctype: str, settings) -> bool:
	"""
	Check if a doctype is configured to only send to remote (send=1, fetch=0)
	
	Args:
		doctype: Document type to check
		settings: Havano Sync Settings
		
	Returns:
		True if doctype is send-only, False otherwise
	"""
	syncable_doctypes = get_syncable_doctypes(settings)
	
	for syncable in syncable_doctypes:
		syncable_doctype = syncable.doctypes
		# Remove -Local suffix if present
		if syncable_doctype and syncable_doctype.endswith("-Local"):
			syncable_doctype = syncable_doctype[:-6]
		
		if syncable_doctype == doctype:
			send = cint(syncable.get('send', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'send', 0))
			fetch = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			# Return True only if send is enabled AND fetch is disabled
			return send == 1 and fetch == 0
	return False


def ensure_sync_status_field_exists(doctype: str) -> bool:
	"""
	Ensure sync_status field exists on local doctype for all syncable doctypes
	Creates the field if it doesn't exist
	
	Args:
		doctype: Document type
		
	Returns:
		True if field exists or was created, False otherwise
	"""
	try:
		# Check if field already exists
		if frappe.db.has_column(doctype, 'sync_status'):
			# Update field options if it exists but has old options
			try:
				doctype_doc = frappe.get_doc("DocType", doctype)
				for field in doctype_doc.fields:
					if field.fieldname == 'sync_status':
						if field.options != "\nPending\nSynced\nFetched":
							field.options = "\nPending\nSynced\nFetched"
							# Update default if not set
							if not field.default:
								field.default = "Pending"
							doctype_doc.save(ignore_permissions=True)
							frappe.db.commit()
							frappe.logger().info(f"Updated sync_status field options for {doctype}")
						break
			except Exception:
				pass
			return True
		
		# Get the doctype meta
		doctype_doc = frappe.get_doc("DocType", doctype)
		
		# Check if sync_status field already exists in fields
		field_exists = any(f.fieldname == 'sync_status' for f in doctype_doc.fields)
		if field_exists:
			return True
		
		# Find the last field index
		max_idx = 0
		for field in doctype_doc.fields:
			idx = field.idx or 0
			if isinstance(idx, (int, float)):
				max_idx = max(max_idx, int(idx))
		
		# Add sync_status field with Pending, Synced, and Fetched options
		doctype_doc.append('fields', {
			"fieldname": "sync_status",
			"fieldtype": "Select",
			"label": "Sync Status",
			"options": "\nPending\nSynced\nFetched",
			"default": "Pending",
			"read_only": 0,
			"no_copy": 1,
			"idx": max_idx + 1
		})
		
		# Save the doctype
		doctype_doc.save(ignore_permissions=True)
		frappe.db.commit()
		
		frappe.logger().info(f"Added sync_status field to {doctype}")
		return True
		
	except Exception as e:
		frappe.log_error(
			title=f"Failed to add sync_status field to {doctype}",
			message=f"Error: {str(e)}\n{frappe.get_traceback()}"
		)
		return False


def has_sync_status(doctype: str, name: str) -> bool:
	"""
	Check if document has sync_status set (Synced or Fetched)
	Documents with "Pending" or empty status should be synced
	
	Args:
		doctype: Document type
		name: Document name
		
	Returns:
		True if sync_status is Synced or Fetched, False otherwise (Pending or empty should sync)
	"""
	try:
		if not frappe.db.has_column(doctype, 'sync_status'):
			return False
		sync_status = frappe.db.get_value(doctype, name, 'sync_status')
		# Only skip if already synced or fetched
		return sync_status in ('Synced', 'Fetched')
	except Exception:
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


def fix_field_options_with_local_suffix():
	"""
	Fix field options that incorrectly reference doctypes with -Local suffix
	This fixes database corruption where field options have doctype names with -Local suffix
	"""
	try:
		# Find all fields where options ends with -Local
		fields_to_fix = frappe.db.sql("""
			SELECT parent, fieldname, options
			FROM `tabDocField`
			WHERE options LIKE '%-Local'
			AND fieldtype = 'Link'
		""", as_dict=True)
		
		if not fields_to_fix:
			frappe.logger().info("No fields found with -Local suffix in options")
			return {
				"status": "success",
				"message": "No fields found with -Local suffix in options",
				"fixed_count": 0
			}
		
		fixed_count = 0
		for field in fields_to_fix:
			original_options = field.options
			# Remove -Local suffix (6 characters)
			fixed_options = original_options[:-6]
			
			# Verify the fixed doctype exists
			if frappe.db.exists("DocType", fixed_options):
				# Update the field options
				frappe.db.set_value(
					"DocField",
					{"parent": field.parent, "fieldname": field.fieldname},
					"options",
					fixed_options
				)
				fixed_count += 1
				frappe.logger().info(
					f"Fixed field {field.parent}.{field.fieldname}: "
					f"options changed from '{original_options}' to '{fixed_options}'"
				)
			else:
				frappe.logger().warning(
					f"Could not fix field {field.parent}.{field.fieldname}: "
					f"doctype '{fixed_options}' does not exist"
				)
		
		# Commit the changes
		frappe.db.commit()
		
		# Clear cache to ensure changes are reflected
		frappe.clear_cache()
		
		return {
			"status": "success",
			"message": f"Fixed {fixed_count} field(s) with -Local suffix in options",
			"fixed_count": fixed_count
		}
		
	except Exception as e:
		frappe.log_error(
			title="Failed to fix field options with -Local suffix",
			message=f"Error fixing field options: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)
		return {
			"status": "error",
			"message": f"Failed to fix field options: {str(e)}",
			"fixed_count": 0
		}


def fix_renamed_doctypes():
	"""
	Fix DocType definitions that were incorrectly renamed with -Local suffix
	This fixes database corruption where DocType definitions have -Local suffix
	"""
	try:
		# First, let's check what DocTypes exist with -Local suffix
		all_doctypes = frappe.db.sql("""
			SELECT name
			FROM `tabDocType`
			WHERE name LIKE '%-Local'
			ORDER BY name
		""", as_dict=True)
		
		frappe.logger().info(f"[FIX_DOCTYPES] Query found {len(all_doctypes) if all_doctypes else 0} DocType(s) with -Local suffix")
		
		if all_doctypes:
			frappe.logger().info(f"[FIX_DOCTYPES] DocTypes found: {[d.name for d in all_doctypes]}")
		
		# Find all DocType definitions that end with -Local
		doctypes_to_fix = all_doctypes
		
		if not doctypes_to_fix:
			# Also check with different pattern in case of encoding issues
			doctypes_to_fix = frappe.db.sql("""
				SELECT name
				FROM `tabDocType`
				WHERE name REGEXP '-Local$'
			""", as_dict=True)
			frappe.logger().info(f"[FIX_DOCTYPES] REGEXP query found {len(doctypes_to_fix) if doctypes_to_fix else 0} DocType(s)")
		
		if not doctypes_to_fix:
			frappe.logger().info("No DocType definitions found with -Local suffix")
			return {
				"status": "success",
				"message": "No DocType definitions found with -Local suffix",
				"fixed_count": 0,
				"fixed_doctypes": [],
				"checked_count": len(all_doctypes) if all_doctypes else 0
			}
		
		fixed_count = 0
		fixed_doctypes = []
		errors = []
		skipped = []
		
		for doctype_info in doctypes_to_fix:
			original_name = doctype_info.name
			# Remove -Local suffix (6 characters)
			fixed_name = original_name[:-6]
			
			frappe.logger().info(f"[FIX_DOCTYPES] Processing: '{original_name}' -> '{fixed_name}'")
			
			# Verify the fixed name doesn't already exist
			if frappe.db.exists("DocType", fixed_name):
				error_msg = f"DocType '{fixed_name}' already exists. Cannot rename '{original_name}' to '{fixed_name}'"
				skipped.append({
					"doctype": original_name,
					"reason": error_msg
				})
				frappe.logger().warning(f"[FIX_DOCTYPES] {error_msg}")
				continue
			
			frappe.logger().info(f"[FIX_DOCTYPES] Target name '{fixed_name}' is available, proceeding with rename")
			
			# First, find and rename all child tables that end with -Local
			# Child tables are DocTypes where istable=1 and they might have been renamed too
			try:
				# Get the DocType document to find child tables
				doctype_doc = frappe.get_doc("DocType", original_name)
				
				# Find all child tables by checking fields in this DocType
				child_tables_to_fix = []
				
				# Check all Table fields in this DocType to find child tables
				for field in doctype_doc.fields:
					if field.fieldtype == "Table" and field.options:
						child_table_name = field.options
						# If the child table name ends with -Local, we need to fix it
						if child_table_name.endswith("-Local"):
							child_fixed = child_table_name[:-6]
							# Check if fixed name already exists
							if not frappe.db.exists("DocType", child_fixed):
								child_tables_to_fix.append({
									"original": child_table_name,
									"fixed": child_fixed
								})
								frappe.logger().info(
									f"[FIX_DOCTYPES] Found child table '{child_table_name}' that needs to be renamed to '{child_fixed}'"
								)
				
				# Rename child tables first (before parent)
				for child_table in child_tables_to_fix:
					try:
						# Check if child table DocType exists
						if not frappe.db.exists("DocType", child_table["original"]):
							frappe.logger().warning(
								f"[FIX_DOCTYPES] Child table DocType '{child_table['original']}' does not exist. Skipping."
							)
							continue
						
						# Check if fixed name already exists
						if frappe.db.exists("DocType", child_table["fixed"]):
							frappe.logger().warning(
								f"[FIX_DOCTYPES] Child table '{child_table['fixed']}' already exists. Skipping '{child_table['original']}'"
							)
							continue
						
						frappe.logger().info(
							f"[FIX_DOCTYPES] Renaming child table '{child_table['original']}' to '{child_table['fixed']}'"
						)
						
						# Rename child table DocType
						frappe.rename_doc("DocType", child_table["original"], child_table["fixed"], force=True, merge=False, show_alert=False)
						frappe.logger().info(
							f"[FIX_DOCTYPES] Successfully renamed child table from '{child_table['original']}' to '{child_table['fixed']}'"
						)
						frappe.db.commit()
						frappe.clear_cache(doctype=child_table["fixed"])
					except Exception as child_error:
						error_msg = f"Failed to rename child table '{child_table['original']}': {str(child_error)}"
						frappe.logger().error(f"[FIX_DOCTYPES] {error_msg}")
						frappe.log_error(
							title=f"Failed to rename child table {child_table['original']}",
							message=f"{error_msg}\nTraceback: {frappe.get_traceback()}"
						)
						# Continue anyway - try to rename parent
				
			except Exception as prep_error:
				error_msg = f"Error preparing to rename '{original_name}': {str(prep_error)}"
				frappe.logger().warning(f"[FIX_DOCTYPES] {error_msg}")
				frappe.log_error(
					title=f"Error preparing DocType rename {original_name}",
					message=f"{error_msg}\nTraceback: {frappe.get_traceback()}"
				)
				# Continue to try renaming anyway
			
			# Now rename the parent DocType definition
			try:
				frappe.logger().info(f"[FIX_DOCTYPES] Starting rename process for '{original_name}' -> '{fixed_name}'")
				
				# Clear cache before rename
				frappe.clear_cache(doctype=original_name)
				frappe.clear_cache()
				
				# Let Frappe's rename_doc handle table renaming automatically
				# It will rename both the DocType and all associated tables
				frappe.logger().info(f"[FIX_DOCTYPES] Calling frappe.rename_doc for '{original_name}' -> '{fixed_name}'")
				frappe.rename_doc("DocType", original_name, fixed_name, force=True, merge=False, show_alert=False)
				fixed_count += 1
				fixed_doctypes.append({
					"original": original_name,
					"fixed": fixed_name
				})
				frappe.logger().info(
					f"[FIX_DOCTYPES] Successfully renamed DocType definition from '{original_name}' to '{fixed_name}'"
				)
				# Commit after each rename to ensure it's saved
				frappe.db.commit()
				# Clear cache after rename
				frappe.clear_cache(doctype=fixed_name)
				frappe.logger().info(f"[FIX_DOCTYPES] Completed rename for '{original_name}' -> '{fixed_name}'")
			except Exception as rename_error:
				error_msg = f"Failed to rename DocType '{original_name}' to '{fixed_name}': {str(rename_error)}"
				error_details = {
					"doctype": original_name,
					"target": fixed_name,
					"error": str(rename_error),
					"traceback": frappe.get_traceback()
				}
				errors.append(error_details)
				frappe.logger().error(f"[FIX_DOCTYPES] {error_msg}")
				# Log to Error Log
				frappe.log_error(
					title=f"Failed to rename DocType {original_name}",
					message=f"{error_msg}\nTraceback: {frappe.get_traceback()}"
				)
				# Also print to console for immediate visibility
				print(f"[FIX_DOCTYPES ERROR] {error_msg}")
				print(f"[FIX_DOCTYPES ERROR] Traceback: {frappe.get_traceback()}")
		
		# Clear cache to ensure changes are reflected
		frappe.clear_cache()
		
		result = {
			"status": "success" if fixed_count > 0 and len(errors) == 0 else ("partial" if fixed_count > 0 else "error"),
			"message": f"Fixed {fixed_count} DocType definition(s) with -Local suffix",
			"fixed_count": fixed_count,
			"fixed_doctypes": fixed_doctypes,
			"total_found": len(doctypes_to_fix) if doctypes_to_fix else 0,
			"error_count": len(errors),
			"skipped_count": len(skipped)
		}
		
		if errors:
			# Include first 10 errors in message, full list in errors array
			error_summary = errors[:10]
			result["errors"] = errors
			result["error_summary"] = [f"{e.get('doctype', 'Unknown')}: {e.get('error', str(e))}" for e in error_summary]
			result["message"] += f". {len(errors)} error(s) occurred."
			if len(errors) > 10:
				result["message"] += f" Showing first 10 errors. Check full error list for all {len(errors)} errors."
		
		if skipped:
			result["skipped"] = skipped
		
		if fixed_count == 0 and len(doctypes_to_fix) > 0:
			result["message"] += f" Found {len(doctypes_to_fix)} DocType(s) but none were fixed."
			if errors:
				result["message"] += f" {len(errors)} error(s) occurred. Check errors array for details."
			if skipped:
				result["message"] += f" {len(skipped)} skipped (target name already exists)."
		
		frappe.logger().info(f"[FIX_DOCTYPES] Final result: Fixed={fixed_count}, Errors={len(errors)}, Skipped={len(skipped)}")
		if errors:
			frappe.logger().error(f"[FIX_DOCTYPES] First 5 errors: {errors[:5]}")
		
		return result
		
	except Exception as e:
		frappe.log_error(
			title="Failed to fix renamed DocTypes",
			message=f"Error fixing renamed DocTypes: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)
		return {
			"status": "error",
			"message": f"Failed to fix renamed DocTypes: {str(e)}",
			"fixed_count": 0,
			"fixed_doctypes": []
		}

