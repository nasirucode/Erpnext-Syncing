# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
import time
import requests
from datetime import date, datetime, timedelta
from frappe.utils import now, cint, now_datetime
from havano_sync.havano_sync.utils.sync_api import SyncAPI, DocumentNotFoundError, DuplicateEntryError
from typing import Optional, Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
	from frappe.model.document import Document


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


def ensure_sync_fields_exist_on_remote(doctype: str, api_client: Any, settings: Any = None) -> bool:
	"""
	Ensure sync_reference and sync_type fields exist on remote doctype
	Creates them if they don't exist
	For all syncable and compulsory doctypes (not just submittable)
	
	Args:
		doctype: Document type
		api_client: SyncAPI client instance
		settings: Havano Sync Settings (optional, will be fetched if not provided)
	
	Returns:
		True if fields exist or were created, False otherwise
	"""
	try:
		# Check if doctype is syncable (in syncable doctypes with send enabled) or is compulsory
		if settings is None:
			settings = get_sync_settings()
		
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		is_compulsory = doctype in auto_sync_doctypes
		is_syncable = should_sync_doctype(doctype, settings, direction="send")
		
		# Only create fields for syncable or compulsory doctypes
		if not is_compulsory and not is_syncable:
			return True  # Not syncable, no need for sync fields
		
		# Try to get the doctype document from remote to check if fields exist
		try:
			# Get the doctype document directly
			doctype_doc = api_client.get_document("DocType", doctype)
			
			# Check if sync_reference and sync_type fields exist
			fields = doctype_doc.get('fields', [])
			if not isinstance(fields, list):
				fields = []
			
			field_names = [f.get('fieldname') for f in fields if f.get('fieldname')]
			
			has_sync_reference = 'sync_reference' in field_names
			has_sync_type = 'sync_type' in field_names
			
			if has_sync_reference and has_sync_type:
				return True  # Fields already exist
			
			# Fields don't exist, create them
			
			# Ensure fields list exists
			if 'fields' not in doctype_doc or not isinstance(doctype_doc.get('fields'), list):
				doctype_doc['fields'] = []
			
			# Find the last field index
			max_idx = 0
			for field in doctype_doc.get('fields', []):
				idx = field.get('idx', 0)
				if isinstance(idx, (int, float)):
					max_idx = max(max_idx, int(idx))
			
			# Add fields if they don't exist
			if not has_sync_reference:
				sync_ref_field = {
					"fieldname": "sync_reference",
					"fieldtype": "Data",
					"label": "Sync Reference",
					"description": "Reference to the corresponding document in remote/local instance",
					"read_only": 1,
					"no_copy": 1,
					"unique": 1,  # Mark as unique to avoid duplication
					"idx": max_idx + 1
				}
				doctype_doc['fields'].append(sync_ref_field)
				max_idx += 1
			
			if not has_sync_type:
				sync_type_field = {
					"fieldname": "sync_type",
					"fieldtype": "Select",
					"label": "Sync Type",
					"options": "Local\nRemote",
					"description": "Indicates whether this document is from Local or Remote instance",
					"read_only": 1,
					"no_copy": 1,
					"default": "Local",
					"idx": max_idx + 1
				}
				doctype_doc['fields'].append(sync_type_field)
			
			# Save the doctype with new fields using frappe.client.save
			# Remove metadata fields before saving
			metadata_fields = {'name', 'doctype', 'modified', 'modified_by', 'creation', 'owner', '_user_tags', '_comments', '_assign', '_liked_by', '_seen'}
			doctype_data = {k: v for k, v in doctype_doc.items() if k not in metadata_fields}
			doctype_data['doctype'] = "DocType"
			doctype_data['name'] = doctype
			
			# Use update_document to save
			api_client.update_document("DocType", doctype, doctype_data)
			
			# Fields have been created, no need to clear cache as update_document handles it
			
			frappe.logger().info(f"Created sync_reference and sync_type fields on remote doctype {doctype}")
			return True
			
		except Exception as e:
			frappe.log_error(
				title="Failed to ensure sync fields on remote",
				message=f"Could not ensure sync fields exist on remote doctype {doctype}: {str(e)}"
			)
			return False
			
	except Exception as e:
		frappe.log_error(
			title="Failed to check/create sync fields",
			message=f"Error checking/creating sync fields for {doctype}: {str(e)}"
		)
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
		if syncable.doctypes == doctype:
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
			elif "Connection error" in error_message or "timeout" in error_message.lower():
				# No internet connection
				return False
		
		return success
	except Exception as e:
		# Check if it's a connection error (no internet) vs auth error (has internet)
		error_str = str(e)
		if "401" in error_str or "Unauthorized" in error_str:
			# Internet is available, just auth failed
			return True
		# Other errors likely mean no internet
		return False


def create_sync_log(
	sync_type: str,
	doctype: str,
	document_name: str,
	status: str,
	message: str = None,
	error_details: str = None,
	direction: str = "Local to Remote",
	sync_method: str = "Auto",
	duration_seconds: float = None
):
	"""Create a log entry in Havano Sync Log"""
	try:
		# Create the log document
		# Note: We can't use "doctype" as a key in the dict because it conflicts with the document doctype
		# So we create the doc first, then set the doctype field
		log_doc = frappe.get_doc({
			"doctype": "Havano Sync Log",
			"sync_type": sync_type,
			"document_name": document_name,
			"status": status,
			"message": message,
			"error_details": json.dumps(error_details) if error_details else None,
			"direction": direction,
			"sync_method": sync_method,
			"sync_timestamp": now_datetime(),
			"duration_seconds": duration_seconds
		})
		# Set the doctype field (the synced doctype, not the log doctype) after creation
		log_doc.set("doctype", doctype)
		log_doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(f"Failed to create sync log: {str(e)}")


def queue_sync_job(
	doctype: str,
	name: str,
	sync_type: str = "Send",
	document_data: Dict[str, Any] = None,
	priority: int = 5
):
	"""Queue a sync job when offline"""
	try:
		# Check if already queued
		existing = frappe.get_all(
			"Havano Sync Queue",
			filters={
				"doctype": doctype,
				"document_name": name,
				"sync_type": sync_type,
				"status": ["in", ["Queued", "Processing"]]
			},
			limit=1
		)
		
		if existing:
			return existing[0]["name"]
		
		# Create the queue document
		# Note: We can't use "doctype" as a key in the dict because it conflicts with the document doctype
		# So we create the doc first, then set the doctype field (the synced doctype, not the queue doctype)
		queue_doc = frappe.get_doc({
			"doctype": "Havano Sync Queue",
			"sync_type": sync_type,
			"document_name": name,
			"status": "Queued",
			"priority": priority,
			"retry_count": 0,
			"max_retries": 3,
			"document_data": serialize_document_data(document_data) if document_data else None,
			"queued_at": now_datetime()
		})
		# Set the doctype field (the synced doctype, not the queue doctype) after creation
		queue_doc.set("doctype", doctype)
		queue_doc.insert(ignore_permissions=True)
		frappe.db.commit()
		
		create_sync_log(
			sync_type=sync_type,
			doctype=doctype,
			document_name=name,
			status="Queued",
			message="Job queued due to offline status",
			sync_method="Queue"
		)
		
		return queue_doc.name
	except Exception as e:
		frappe.log_error(f"Failed to queue sync job: {str(e)}")
	return None


def prepare_doc_for_sync(doc) -> Dict[str, Any]:
	"""Prepare document data for syncing (remove internal fields and convert date/datetime)"""
	doc_dict = doc.as_dict()
	
		# Remove internal fields that shouldn't be synced
	exclude_fields = [
		'creation', 'modified', 'modified_by', 'owner', 
		'idx', 'doctype', 'name',
		'_user_tags', '_comments', '_assign', '_liked_by',
		'__islocal', '__unsaved', '__run_link_triggers'
	]
	# Note: docstatus is NOT excluded - we need it for submittable doctypes
	
	# Also exclude child table internal fields
	for key in list(doc_dict.keys()):
		if key in exclude_fields or key.startswith('_'):
			doc_dict.pop(key, None)
		# Convert date/datetime/timedelta objects to ISO format strings for JSON serialization
		elif isinstance(doc_dict[key], (date, datetime)):
			doc_dict[key] = doc_dict[key].isoformat()
		elif isinstance(doc_dict[key], timedelta):
			# Convert timedelta to HH:MM:SS format (Frappe time format)
			total_seconds = int(doc_dict[key].total_seconds())
			hours = total_seconds // 3600
			minutes = (total_seconds % 3600) // 60
			seconds = total_seconds % 60
			doc_dict[key] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	
	# Handle child tables - they are already in the dict as lists
	# Just need to clean up internal fields from each child row
	for field in doc.meta.fields:
		if field.fieldtype == "Table" and field.fieldname in doc_dict:
			fieldname = field.fieldname
			if isinstance(doc_dict[fieldname], list):
				child_table_data = []
				for child_dict in doc_dict[fieldname]:
					if isinstance(child_dict, dict):
						# Remove internal fields from child table
						cleaned_child = {}
						for child_key, child_value in child_dict.items():
							if child_key not in exclude_fields and not child_key.startswith('_'):
								# Convert date/datetime/timedelta objects to ISO format strings
								if isinstance(child_value, (date, datetime)):
									cleaned_child[child_key] = child_value.isoformat()
								elif isinstance(child_value, timedelta):
									# Convert timedelta to HH:MM:SS format (Frappe time format)
									total_seconds = int(child_value.total_seconds())
									hours = total_seconds // 3600
									minutes = (total_seconds % 3600) // 60
									seconds = total_seconds % 60
									cleaned_child[child_key] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
								else:
									cleaned_child[child_key] = child_value
						# Add doctype for child table
						cleaned_child['doctype'] = field.options
						child_table_data.append(cleaned_child)
				doc_dict[fieldname] = child_table_data
	
	return doc_dict


def create_minimal_master_document(doctype: str, name: str) -> Optional["Document"]:
	"""
	Create a minimal master document with required fields only
	
	Args:
		doctype: The doctype to create
		name: The document name
		
	Returns:
		The created document or None if creation failed
	"""
	try:
		# Check if document already exists
		try:
			existing_doc = frappe.get_doc(doctype, name)
			return existing_doc
		except frappe.DoesNotExistError:
			pass
		
		# Get meta to understand required fields
		meta = frappe.get_meta(doctype)
		
		# Create minimal document based on doctype
		doc_data = {"doctype": doctype, "name": name}
		
		if doctype == "Company":
			# Company requires: company_name, abbr, default_currency
			doc_data["company_name"] = name
			# Extract abbreviation from name (e.g., "Havanno" -> "H")
			abbr = name.split()[-1] if " " in name else name[:1].upper()
			doc_data["abbr"] = abbr
			doc_data["default_currency"] = frappe.db.get_single_value("System Settings", "currency") or "USD"
			# Get default country
			doc_data["country"] = frappe.db.get_single_value("System Settings", "country") or "United States"
		
		elif doctype == "Account":
			# Account requires: account_name, parent_account, account_type, company
			doc_data["account_name"] = name
			# Get company from context or use default
			company = frappe.db.get_single_value("Global Defaults", "default_company")
			if not company:
				companies = frappe.get_all("Company", limit=1)
				if companies:
					company = companies[0].name
			if company:
				doc_data["company"] = company
			# Try to find a root account for this company or create as root
			if company:
				root_accounts = frappe.get_all("Account", filters={"is_group": 1, "parent_account": "", "company": company}, limit=1)
				if root_accounts:
					doc_data["parent_account"] = root_accounts[0].name
				else:
					doc_data["is_group"] = 1
			else:
				doc_data["is_group"] = 1
			# Default account type based on name
			if "Asset" in name or "Funds" in name:
				doc_data["account_type"] = "Asset"
			elif "Income" in name or "Sales" in name:
				doc_data["account_type"] = "Income"
			elif "Expense" in name or "Cost" in name:
				doc_data["account_type"] = "Expense"
			elif "Liability" in name or "Payable" in name or "Creditor" in name:
				doc_data["account_type"] = "Liability"
			else:
				doc_data["account_type"] = "Asset"  # Default
		
		elif doctype == "Cost Center":
			# Cost Center requires: cost_center_name, parent_cost_center
			doc_data["cost_center_name"] = name
			# Try to find a root cost center
			root_cc = frappe.get_all("Cost Center", filters={"is_group": 1, "parent_cost_center": ""}, limit=1)
			if root_cc:
				doc_data["parent_cost_center"] = root_cc[0].name
			else:
				doc_data["is_group"] = 1
			# Get company from context if available
			company = frappe.db.get_single_value("Global Defaults", "default_company")
			if company:
				doc_data["company"] = company
		
		elif doctype == "Warehouse":
			# Warehouse requires: warehouse_name, company
			doc_data["warehouse_name"] = name
			company = frappe.db.get_single_value("Global Defaults", "default_company")
			if company:
				doc_data["company"] = company
			else:
				# Try to get any company
				companies = frappe.get_all("Company", limit=1)
				if companies:
					doc_data["company"] = companies[0].name
		
		elif doctype == "Currency":
			# Currency requires: currency_name
			doc_data["currency_name"] = name
		
		elif doctype == "UOM":
			# UOM (Unit of Measure) requires: uom_name
			doc_data["uom_name"] = name
		
		elif doctype == "Item Group":
			# Item Group requires: item_group_name, parent_item_group
			doc_data["item_group_name"] = name
			root_groups = frappe.get_all("Item Group", filters={"is_group": 1, "parent_item_group": ""}, limit=1)
			if root_groups:
				doc_data["parent_item_group"] = root_groups[0].name
			else:
				doc_data["is_group"] = 1
		
		# Create the document
		doc = frappe.get_doc(doc_data)
		doc.insert(ignore_permissions=True, ignore_links=True)  # Use ignore_links to skip validation
		frappe.db.commit()
		frappe.logger().info(f"Created minimal {doctype} {name}")
		return doc
		
	except Exception as e:
		frappe.log_error(
			title="Failed to create minimal master document",
			message=f"Could not create minimal {doctype} {name}: {str(e)}"
		)
		return None


def handle_link_validation_error(
	error: Exception,
	doctype: str, 
	name: str, 
	api_client: Any,
	settings: Any,
	target_url: str, 
	api_key: str, 
	api_secret: str,
	direction: str = "send"
) -> bool:
	"""
	Handle LinkValidationError by creating missing linked documents
	
	Args:
		error: The LinkValidationError exception
		doctype: The doctype that failed validation
		name: The document name that failed validation
		api_client: SyncAPI client instance
		settings: Havano Sync Settings
		target_url: Remote server URL
		api_key: API key for authentication
		api_secret: API secret for authentication
		direction: "send" (to remote) or "fetch" (from remote)
	
	Returns:
		True if missing documents were created and operation should be retried, False otherwise
	"""
	error_msg = str(error)
	# Clean up error message: remove newlines, extra quotes, and whitespace
	error_msg = error_msg.replace("\n", " ").replace('\\n', " ").replace('"]', "").replace('"', "").strip()
	frappe.logger().info(f"Handling LinkValidationError for {doctype} {name}: {error_msg}")
	
	# Parse error message to extract missing documents
	# Format: "Could not find Default Cash Account: Cash - IC B, Default Receivable Account: Debtors - IC B, ..."
	# Or: "Could not find Company: Havanno, Parent Account: Bank Accounts - H"
	missing_docs = []
	
	if "Could not find" in error_msg:
		# Extract the part after "Could not find"
		missing_part = error_msg.split("Could not find", 1)[1].strip()
		
		# Split by comma to get individual missing items
		items = [item.strip() for item in missing_part.split(",")]
		
		# Get document meta to find field definitions
		try:
			meta = frappe.get_meta(doctype)
		except Exception:
			frappe.log_error(
				title="Failed to get meta for LinkValidationError handling",
				message=f"Could not get meta for {doctype}: {str(error)}"
			)
			return False
		
		# Parse each item: "Field Label: Document Name"
		for item in items:
			if ":" in item:
				parts = item.split(":", 1)
				if len(parts) == 2:
					field_label = parts[0].strip()
					doc_name = parts[1].strip()
					
					# Clean up doc_name: remove newlines, quotes, and extra whitespace
					doc_name = doc_name.replace("\n", "").replace('"', "").replace("'", "").strip()
					# Remove trailing ] if present (from JSON parsing)
					if doc_name.endswith("]"):
						doc_name = doc_name[:-1].strip()
					
					# Find the field by label to get doctype
					link_doctype = None
					for field in meta.fields:
						if field.label == field_label and field.fieldtype == "Link":
							link_doctype = field.options
							break
					
					if link_doctype and doc_name:
						missing_docs.append({
							"doctype": link_doctype,
							"name": doc_name,
							"field_label": field_label
						})
	
	if not missing_docs:
		frappe.logger().warning(f"Could not parse missing documents from LinkValidationError: {error_msg}")
		return False
	
	# Try to create each missing document
	created_count = 0
	for missing_doc in missing_docs:
		link_doctype = missing_doc["doctype"]
		link_name = missing_doc["name"]
		field_label = missing_doc["field_label"]
		
		frappe.logger().info(f"Attempting to create missing document {link_doctype} {link_name} (referenced in {field_label})")
		
		# Try to get from local first
		doc_created = False
		try:
			local_doc = frappe.get_doc(link_doctype, link_name)
			# Document exists locally
			if direction == "send":
				# Sync it to remote
				try:
					sync_result = sync_document_to_remote(
						link_doctype, link_name, target_url,
						api_key, api_secret, force_create=True,
						sync_method="Auto", settings=settings
					)
					if sync_result.get("status") == "success":
						doc_created = True
						created_count += 1
						frappe.logger().info(f"Created missing document {link_doctype} {link_name} on remote from local")
				except Exception as sync_error:
					frappe.log_error(
						title="Failed to sync missing document to remote",
						message=f"Could not sync {link_doctype} {link_name} to remote: {str(sync_error)}"
					)
			else:  # fetch direction - document already exists locally
				doc_created = True
				created_count += 1
				frappe.logger().info(f"Missing document {link_doctype} {link_name} already exists locally")
		except frappe.DoesNotExistError:
			# Document doesn't exist locally, try to fetch from remote
			if direction == "fetch":
				try:
					fetch_result = fetch_document_from_remote(link_doctype, link_name)
					if fetch_result.get("status") == "success":
						doc_created = True
						created_count += 1
						frappe.logger().info(f"Created missing document {link_doctype} {link_name} locally from remote")
				except Exception as fetch_error:
					frappe.log_error(
						title="Failed to fetch missing document from remote",
						message=f"Could not fetch {link_doctype} {link_name} from remote: {str(fetch_error)}"
					)
			else:  # send direction - document doesn't exist locally or remote
				# For master doctypes, create a minimal document
				master_doctypes = {'Company', 'Account', 'Cost Center', 'Warehouse', 'Currency', 'UOM', 'Item Group'}
				if link_doctype in master_doctypes:
					try:
						frappe.logger().info(f"Creating minimal {link_doctype} {link_name} as master doctype")
						# Create minimal document locally first
						minimal_doc = create_minimal_master_document(link_doctype, link_name)
						if minimal_doc:
							# Now sync it to remote
							try:
								sync_result = sync_document_to_remote(
									link_doctype, link_name, target_url,
									api_key, api_secret, force_create=True,
									sync_method="Auto", settings=settings
								)
								if sync_result.get("status") == "success":
									doc_created = True
									created_count += 1
									frappe.logger().info(f"Created minimal master document {link_doctype} {link_name} on remote")
							except Exception as sync_error:
								frappe.log_error(
									title="Failed to sync minimal master document to remote",
									message=f"Could not sync minimal {link_doctype} {link_name} to remote: {str(sync_error)}"
								)
					except Exception as create_error:
						frappe.log_error(
							title="Failed to create minimal master document",
							message=f"Could not create minimal {link_doctype} {link_name}: {str(create_error)}"
						)
				else:
					frappe.log_error(
						title="Missing document not found",
						message=f"Missing document {link_doctype} {link_name} does not exist locally or on remote. Cannot create."
					)
		
		if not doc_created:
			frappe.logger().warning(f"Could not create missing document {link_doctype} {link_name}")
	
	if created_count > 0:
		frappe.logger().info(f"Created {created_count} missing document(s) for {doctype} {name}. Operation should be retried.")
		return True
	
	return False


def sync_linked_documents(
	doc: "Document",
	api_client: Any,
	settings: Any,
	target_url: str,
	api_key: str,
	api_secret: str,
	synced_docs: Optional[set] = None,
	direction: str = "send"
) -> None:
	"""
	Recursively sync linked documents that are referenced by the current document
	This ensures all dependencies exist on the remote server before syncing the main document
	
	Args:
		doc: The document to check for linked documents
		api_client: SyncAPI client instance
		settings: Havano Sync Settings
		target_url: Remote server URL
		api_key: API key for authentication
		api_secret: API secret for authentication
		synced_docs: Set of (doctype, name) tuples already synced to prevent infinite loops
		direction: "send" (to remote) or "fetch" (from remote)
	"""
	if synced_docs is None:
		synced_docs = set()
	
	# Get enabled doctypes for sync based on direction
	enabled_doctypes = set()
	if not settings:
		settings = get_sync_settings()
	if settings and hasattr(settings, 'syncable_doctypes'):
		for syncable in settings.syncable_doctypes:
			# Check if the appropriate direction is enabled
			if direction == "send":
				enabled = cint(syncable.get('send', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'send', 0))
			else:  # fetch
				enabled = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			
			if enabled:
				# Use 'doctypes' field name from child table
				doctype_name = syncable.get('doctypes') if hasattr(syncable, 'get') else getattr(syncable, 'doctypes', None)
				if doctype_name:
					enabled_doctypes.add(doctype_name)
	
	# Track link fields in the document
	link_fields = []
	
	# Get link fields from meta
	for field in doc.meta.fields:
		if field.fieldtype == "Link" and field.options:
			link_doctype = field.options
			link_value = doc.get(field.fieldname)
			
			# Sync linked documents if they exist and are either enabled OR are critical dependencies
			# Critical dependencies include: Account, Cost Center, Warehouse, etc.
			critical_doctypes = {'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group'}
			should_sync = link_value and (link_doctype in enabled_doctypes or link_doctype in critical_doctypes)
			
			if should_sync:
				# Check if this linked document exists on remote
				exists_on_remote = False
				try:
					api_client.get_document(link_doctype, link_value)
					# Document exists, no need to sync
					exists_on_remote = True
					frappe.logger().debug(f"Linked document {link_doctype} {link_value} already exists on remote")
				except (DocumentNotFoundError, requests.exceptions.HTTPError) as e:
					# Document doesn't exist on remote (404 or DocumentNotFoundError)
					exists_on_remote = False
				except Exception as e:
					# Other error occurred - log but continue
					frappe.logger().warning(f"Error checking if {link_doctype} {link_value} exists on remote: {str(e)}")
					exists_on_remote = False
				
				# If document doesn't exist on remote, try to sync/fetch it
				if not exists_on_remote:
					# Check if we've already synced this document in this recursion
					doc_key = (link_doctype, link_value)
					if doc_key not in synced_docs:
						synced_docs.add(doc_key)
						
						# Try to get the linked document locally first
						exists_locally = False
						linked_doc = None
						try:
							linked_doc = frappe.get_doc(link_doctype, link_value)
							exists_locally = True
						except frappe.DoesNotExistError:
							exists_locally = False
						
						if exists_locally:
							# Document exists locally but not on remote - sync it
							try:
								frappe.logger().info(f"Syncing linked document {link_doctype} {link_value} as dependency for {doc.doctype} {doc.name} (exists locally, not on remote)")
								# Recursively sync this linked document first (to handle its dependencies)
								sync_linked_documents(
									linked_doc, api_client, settings, 
									target_url, api_key, api_secret, synced_docs, direction=direction
								)
								# Now sync the linked document itself to remote
								if direction == "send":
									sync_document_to_remote(
										link_doctype, link_value, target_url, 
										api_key, api_secret, force_create=True,
										sync_method="Auto", settings=settings
									)
									frappe.logger().info(f"Successfully synced linked document {link_doctype} {link_value} to remote as dependency")
								else:  # fetch direction - but document exists locally, so no need to fetch
									frappe.logger().info(f"Linked document {link_doctype} {link_value} exists locally, skipping fetch")
							except Exception as sync_error:
								frappe.log_error(
									title="Failed to sync linked document",
									message=f"Could not sync linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name}: {str(sync_error)}"
								)
						else:
							# Document doesn't exist locally either
							# For fetch direction, try to fetch it from remote
							if direction == "fetch":
								try:
									frappe.logger().info(f"Fetching linked document {link_doctype} {link_value} from remote as dependency for {doc.doctype} {doc.name}")
									fetch_result = fetch_document_from_remote(link_doctype, link_value)
									if fetch_result.get("status") == "success":
										frappe.logger().info(f"Successfully fetched linked document {link_doctype} {link_value} from remote")
									else:
										frappe.log_error(
											title="Failed to fetch linked document from remote",
											message=f"Could not fetch linked document {link_doctype} {link_value} from remote: {fetch_result.get('message', 'Unknown error')}"
										)
								except Exception as fetch_error:
									frappe.log_error(
										title="Failed to fetch linked document from remote",
										message=f"Could not fetch linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name}: {str(fetch_error)}"
									)
							else:  # send direction
								# Document doesn't exist locally or on remote - can't sync it
								frappe.log_error(
									title="Linked document not found",
									message=f"Linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name} does not exist locally or on remote. Cannot sync."
								)
	
	# Also check child table link fields
	for field in doc.meta.fields:
		if field.fieldtype == "Table" and field.fieldname in doc.as_dict():
			child_table = doc.get(field.fieldname)
			if child_table:
				child_meta = frappe.get_meta(field.options)
				for child_row in child_table:
					for child_field in child_meta.fields:
						if child_field.fieldtype == "Link" and child_field.options:
							link_doctype = child_field.options
							link_value = child_row.get(child_field.fieldname)
							
							# Sync linked documents if they exist and are either enabled OR are critical dependencies
							critical_doctypes = {'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group'}
							should_sync = link_value and (link_doctype in enabled_doctypes or link_doctype in critical_doctypes)
							
							if should_sync:
								# Check if this linked document exists on remote
								exists_on_remote = False
								try:
									api_client.get_document(link_doctype, link_value)
									exists_on_remote = True
									frappe.logger().debug(f"Linked document {link_doctype} {link_value} from child table already exists on remote")
								except (DocumentNotFoundError, requests.exceptions.HTTPError) as e:
									exists_on_remote = False
								except Exception as e:
									frappe.logger().warning(f"Error checking if {link_doctype} {link_value} exists on remote: {str(e)}")
									exists_on_remote = False
								
								# If document doesn't exist on remote, try to sync/fetch it
								if not exists_on_remote:
									doc_key = (link_doctype, link_value)
									if doc_key not in synced_docs:
										synced_docs.add(doc_key)
										
										# Try to get the linked document locally first
										exists_locally = False
										linked_doc = None
										try:
											linked_doc = frappe.get_doc(link_doctype, link_value)
											exists_locally = True
										except frappe.DoesNotExistError:
											exists_locally = False
										
										if exists_locally:
											# Document exists locally but not on remote - sync it
											try:
												frappe.logger().info(f"Syncing linked document {link_doctype} {link_value} from child table {field.fieldname} as dependency for {doc.doctype} {doc.name}")
												sync_linked_documents(
													linked_doc, api_client, settings,
													target_url, api_key, api_secret, synced_docs, direction=direction
												)
												if direction == "send":
													sync_document_to_remote(
														link_doctype, link_value, target_url,
														api_key, api_secret, force_create=True,
														sync_method="Auto", settings=settings
													)
													frappe.logger().info(f"Successfully synced linked document {link_doctype} {link_value} from child table to remote")
												else:  # fetch direction - but document exists locally
													frappe.logger().info(f"Linked document {link_doctype} {link_value} from child table exists locally, skipping fetch")
											except Exception as sync_error:
												frappe.log_error(
													title="Failed to sync linked document from child table",
													message=f"Could not sync linked document {link_doctype} {link_value} from {field.fieldname} in {doc.doctype} {doc.name}: {str(sync_error)}"
												)
										else:
											# Document doesn't exist locally either
											if direction == "fetch":
												try:
													frappe.logger().info(f"Fetching linked document {link_doctype} {link_value} from child table from remote")
													fetch_result = fetch_document_from_remote(link_doctype, link_value)
													if fetch_result.get("status") == "success":
														frappe.logger().info(f"Successfully fetched linked document {link_doctype} {link_value} from child table")
												except Exception as fetch_error:
													frappe.log_error(
														title="Failed to fetch linked document from child table",
														message=f"Could not fetch linked document {link_doctype} {link_value} from {field.fieldname}: {str(fetch_error)}"
													)
											else:  # send direction
												frappe.log_error(
													title="Linked document from child table not found",
													message=f"Linked document {link_doctype} {link_value} from {field.fieldname} in {doc.doctype} {doc.name} does not exist locally or on remote"
												)


def sync_document_to_remote(
	doctype: str, 
	name: str, 
	target_url: str, 
	api_key: str, 
	api_secret: str = None,
	force_create: bool = False,
	sync_method: str = "Auto",
	settings: Any = None
) -> Dict[str, Any]:
	"""
	Sync a document to the remote instance
	"""
	start_time = time.time()
	
	try:
		# Get decrypted API secret if not provided
		if not api_secret and settings:
			api_secret = get_decrypted_api_secret(settings)
		elif not api_secret:
			# Get settings and decrypt secret
			settings = get_sync_settings()
			api_secret = get_decrypted_api_secret(settings)
		
		if not api_secret:
			raise ValueError("API Secret is not configured or could not be decrypted")
		
		# Initialize API client first (needed for checking linked documents)
		api_client = SyncAPI(target_url, api_key, api_secret)
		
		# Get the document
		doc = frappe.get_doc(doctype, name)
		
		# Sync linked documents first (dependencies) - for send direction
		# This ensures all referenced documents exist on the remote server
		# Skip if this is already a dependency sync (to avoid infinite recursion)
		if not force_create:
			try:
				sync_linked_documents(
					doc, api_client, settings, target_url, api_key, api_secret, direction="send"
				)
			except Exception as e:
				# Log but don't fail - we'll try to sync anyway
				frappe.log_error(
					title="Error syncing linked documents",
					message=f"Error syncing linked documents for {doctype} {name}: {str(e)}"
				)
		
		# Ensure sync fields exist on remote for syncable and compulsory doctypes
		try:
			ensure_sync_fields_exist_on_remote(doctype, api_client, settings)
		except Exception as e:
			# Log but don't fail - we'll try to sync anyway
			frappe.log_error(
				title="Failed to ensure sync fields",
				message=f"Could not ensure sync fields exist on remote for {doctype}: {str(e)}"
			)
		
		# Prepare document data
		doc_data = prepare_doc_for_sync(doc)
		doc_data['doctype'] = doctype
		
		# For all syncable and compulsory doctypes, add sync_reference and sync_type
		# Check if doctype is syncable or compulsory
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		is_compulsory = doctype in auto_sync_doctypes
		is_syncable = should_sync_doctype(doctype, settings, direction="send") if settings else False
		
		if is_compulsory or is_syncable:
			# Set sync_reference to local document name
			doc_data['sync_reference'] = name
			# Set sync_type to "Local" (since this is being sent from local)
			doc_data['sync_type'] = "Local"
			# For submittable doctypes, include docstatus to ensure it's synced as submitted
			if is_submittable_doctype(doctype) and hasattr(doc, 'docstatus'):
				doc_data['docstatus'] = doc.docstatus
		
		# Clean up None values and empty strings that might cause issues
		# Convert None to empty string for string fields, remove None from dict
		def clean_data(data):
			"""Recursively clean data to remove None values that might cause issues"""
			if isinstance(data, dict):
				cleaned = {}
				for k, v in data.items():
					if v is None:
						# Skip None values to avoid validation issues
						continue
					elif isinstance(v, (dict, list)):
						cleaned[k] = clean_data(v)
					else:
						cleaned[k] = v
				return cleaned
			elif isinstance(data, list):
				return [clean_data(item) for item in data if item is not None]
			return data
		
		doc_data = clean_data(doc_data)
		
		# Check if document exists on remote
		doc_exists = False
		existing_doc_by_reference = None
		
		# For all syncable and compulsory doctypes, check if document exists by sync_reference
		# This prevents duplicates when syncing from local
		if is_compulsory or is_syncable:
			try:
				# Search for document with matching sync_reference and sync_type = "Local"
				# (since we're sending from local, we want to find documents we already sent)
				endpoint = "frappe.client.get_list"
				params = {
					"doctype": doctype,
					"filters": json.dumps({"sync_reference": name, "sync_type": "Local"}),
					"limit_page_length": 1
				}
				existing_docs = api_client._make_request("GET", endpoint, params=params)
				if existing_docs and isinstance(existing_docs, list) and len(existing_docs) > 0:
					existing_doc_by_reference = existing_docs[0]
					doc_exists = True
					frappe.logger().info(f"Found existing document {doctype} {existing_doc_by_reference.get('name')} by sync_reference {name}")
			except Exception as ref_check_error:
				# If reference check fails, fall back to normal check
				frappe.logger().debug(f"Could not check by sync_reference: {str(ref_check_error)}")
		
		if not doc_exists and not force_create:
			try:
				from havano_sync.havano_sync.utils.sync_api import DocumentNotFoundError
				remote_doc = api_client.get_document(doctype, name)
				doc_exists = True
				existing_doc_by_reference = {"name": name}
			except DocumentNotFoundError:
				# Document doesn't exist - this is expected, will create it
				doc_exists = False
			except:
				# Other errors (connection, auth, etc.) - try to create anyway
				doc_exists = False
		
		# Create or update document
		if doc_exists and not force_create:
			# Use the existing document name (might be different if found by reference)
			update_name = existing_doc_by_reference.get('name') if existing_doc_by_reference else name
			doc_data['name'] = update_name
			try:
				result = api_client.update_document(doctype, update_name, doc_data)
				action = "updated"
			except requests.exceptions.HTTPError as update_error:
				# Check if it's a LinkValidationError
				error_str = str(update_error)
				if "LinkValidationError" in error_str or "Could not find" in error_str:
					# Try to handle missing linked documents
					frappe.logger().info(f"LinkValidationError detected during update for {doctype} {name}, attempting to create missing documents")
					handled = handle_link_validation_error(
						update_error, doctype, name, api_client, settings,
						target_url, api_key, api_secret, direction="send"
					)
					if handled:
						# Retry the update operation
						try:
							result = api_client.update_document(doctype, name, doc_data)
							action = "updated"
							frappe.logger().info(f"Successfully updated {doctype} {name} after handling LinkValidationError")
						except Exception as retry_error:
							# If retry still fails, raise the original error
							raise update_error
					else:
						# Could not handle the error, raise it
						raise update_error
				else:
					# Re-raise other errors
					raise
		else:
			# For new documents, use the same name if possible
			# But for submittable doctypes, we might want to let remote generate the name
			# to avoid conflicts, or use sync_reference to track
			# However, we should keep the name if sync_reference is set, as it helps with tracking
			if is_submittable_doctype(doctype) and (is_compulsory or is_syncable):
				# For syncable submittable doctypes, don't set name - let remote generate it
				# The sync_reference will help us find it later
				if 'name' in doc_data:
					del doc_data['name']
			else:
				doc_data['name'] = name
			
			try:
				result = api_client.create_document(doctype, doc_data)
				action = "created"
				
				# After creating, ensure sync_reference and sync_type are set on remote
				# This is important because remote might have generated a new name
				if (is_compulsory or is_syncable) and result and isinstance(result, dict):
					created_name = result.get('name') or result.get('data', {}).get('name')
					if created_name and created_name != name:
						# Remote generated a new name, update sync_reference to point to local name
						try:
							update_data = {
								'sync_reference': name,
								'sync_type': 'Local'
							}
							api_client.update_document(doctype, created_name, update_data)
							frappe.logger().info(f"Updated sync_reference={name} and sync_type=Local on remote {doctype} {created_name}")
						except Exception as update_error:
							frappe.logger().warning(f"Could not update sync_reference on remote {doctype} {created_name}: {str(update_error)}")
			except DuplicateEntryError:
				# Document already exists - treat as success and update it instead
				frappe.logger().info(f"Document {doctype} {name} already exists on remote. Updating instead.")
				result = api_client.update_document(doctype, name, doc_data)
				action = "updated"
			except requests.exceptions.HTTPError as create_error:
				# Check if it's a LinkValidationError
				error_str = str(create_error)
				if "LinkValidationError" in error_str or "Could not find" in error_str:
					# Try to handle missing linked documents
					frappe.logger().info(f"LinkValidationError detected for {doctype} {name}, attempting to create missing documents")
					handled = handle_link_validation_error(
						create_error, doctype, name, api_client, settings,
						target_url, api_key, api_secret, direction="send"
					)
					if handled:
						# Retry the create operation
						try:
							result = api_client.create_document(doctype, doc_data)
							action = "created"
							frappe.logger().info(f"Successfully created {doctype} {name} after handling LinkValidationError")
							
							# After creating, ensure sync_reference and sync_type are set on remote
							if (is_compulsory or is_syncable) and result and isinstance(result, dict):
								created_name = result.get('name') or result.get('data', {}).get('name')
								if created_name:
									try:
										update_data = {
											'sync_reference': name,
											'sync_type': 'Local'
										}
										api_client.update_document(doctype, created_name, update_data)
										frappe.logger().info(f"Updated sync_reference={name} and sync_type=Local on remote {doctype} {created_name}")
									except Exception as update_error:
										frappe.logger().warning(f"Could not update sync_reference on remote {doctype} {created_name}: {str(update_error)}")
						except Exception as retry_error:
							# If retry still fails, raise the original error
							raise create_error
					else:
						# Could not handle the error, raise it
						raise create_error
				elif "DuplicateEntryError" in error_str or "Duplicate entry" in error_str or "already exists" in error_str.lower():
					# Document already exists - treat as success and update it instead
					frappe.logger().info(f"Document {doctype} {name} already exists on remote. Updating instead.")
					result = api_client.update_document(doctype, name, doc_data)
					action = "updated"
				else:
					# Re-raise other errors
					raise
			except Exception as create_error:
				# Check if it's a duplicate entry error in the error message
				if "DuplicateEntryError" in str(type(create_error)) or "Duplicate entry" in str(create_error) or "already exists" in str(create_error).lower():
					# Document already exists - treat as success and update it instead
					frappe.logger().info(f"Document {doctype} {name} already exists on remote. Updating instead.")
					result = api_client.update_document(doctype, name, doc_data)
					action = "updated"
				else:
					# Re-raise other errors
					raise
		
		duration = time.time() - start_time
		
		# Log success
		create_sync_log(
			sync_type="Send",
			doctype=doctype,
			document_name=name,
			status="Success",
			message=f"Document {action} successfully on remote instance",
			sync_method=sync_method,
			duration_seconds=duration
		)
		
		frappe.logger().info(f"Document {doctype} {name} {action} on remote instance")
		
		# After successful send, check if fetch is also enabled and trigger it
		if settings and should_sync_doctype(doctype, settings, direction="fetch"):
			try:
				# Trigger fetch for this doctype (will skip documents that already exist locally)
				frappe.logger().info(f"Fetch is enabled for {doctype}. Triggering fetch after successful send.")
				# Use enqueue to avoid blocking the send operation
				frappe.enqueue(
					"havano_sync.havano_sync.tasks.sync.fetch_all_documents_from_remote",
					doctype=doctype,
					queue="default",
					timeout=300,
					is_async=True
				)
			except Exception as fetch_error:
				# Log but don't fail the send operation
				frappe.log_error(
					title="Failed to trigger fetch after send",
					message=f"Error triggering fetch for {doctype} after successful send: {str(fetch_error)}"
				)
		
		return {
			"status": "success",
			"action": action,
			"doctype": doctype,
			"name": name,
			"result": result
		}
	
	except Exception as e:
		duration = time.time() - start_time
		error_msg = str(e)
		traceback = frappe.get_traceback()
		
		# Try to extract more detailed error from HTTPError
		detailed_error = error_msg
		if hasattr(e, 'response') and e.response is not None:
			try:
				error_response = e.response.json()
				if 'exc' in error_response:
					detailed_error = f"{error_msg}\nRemote server error: {error_response['exc']}"
				elif 'message' in error_response:
					detailed_error = f"{error_msg}\nRemote server message: {error_response['message']}"
				elif 'exception' in error_response:
					detailed_error = f"{error_msg}\nRemote server exception: {error_response['exception']}"
			except:
				# If JSON parsing fails, try to get text
				try:
					detailed_error = f"{error_msg}\nRemote server response: {e.response.text[:500]}"
				except:
					pass
		
		# Log error
		create_sync_log(
			sync_type="Send",
			doctype=doctype,
			document_name=name,
			status="Failed",
			message=f"Sync failed: {detailed_error}",
			error_details={"error": error_msg, "detailed_error": detailed_error, "traceback": traceback},
			sync_method=sync_method,
			duration_seconds=duration
		)
		
		frappe.log_error(
			f"Sync Failed: {doctype} {name}",
			traceback
		)
		
		return {
			"status": "error",
			"doctype": doctype,
			"name": name,
			"error": detailed_error
		}


def sync_document_on_create(doc, method: Optional[str] = None):
	"""
	Sync document when it's created
	This is called via doc_events hook
	Only works on Local server to send to Remote
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# (e.g., Error Log, Activity Log, etc.)
		system_doctypes = [
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings"
		]
		
		if doctype in system_doctypes:
			return
		
		# For submittable doctypes, skip on_create hook entirely
		# They will be synced via on_submit hook instead to avoid double syncing
		if is_submittable_doctype(doctype):
			return  # Skip submittable doctypes in on_create, they will sync on_submit
		
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return
		
		# Auto-sync doctypes that should always sync on create
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Only sync if it's an auto-sync doctype OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# For all syncable and compulsory doctypes, ensure sync fields exist locally and set them
		# This must be done before syncing
		if should_auto_sync or is_enabled_for_send:
			try:
				# Ensure fields exist locally
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if not has_sync_reference or not has_sync_type:
					# Add fields to local doctype
					doctype_doc = frappe.get_doc("DocType", doctype)
					
					if not has_sync_reference:
						doctype_doc.append("fields", {
							"fieldname": "sync_reference",
							"fieldtype": "Data",
							"label": "Sync Reference",
							"description": "Reference to the corresponding document in remote/local instance",
							"read_only": 1,
							"no_copy": 1,
							"unique": 1  # Mark as unique to avoid duplication
						})
					
					if not has_sync_type:
						doctype_doc.append("fields", {
							"fieldname": "sync_type",
							"fieldtype": "Select",
							"label": "Sync Type",
							"options": "Local\nRemote",
							"description": "Indicates whether this document is from Local or Remote instance",
							"read_only": 1,
							"no_copy": 1,
							"default": "Local"
						})
					
					doctype_doc.save(ignore_permissions=True)
					frappe.db.commit()
					frappe.logger().info(f"Created sync_reference and sync_type fields on local doctype {doctype}")
					# Reload meta
					frappe.clear_cache(doctype=doctype)
					frappe.clear_cache()
					frappe.clear_cache(doctype=doctype)
					frappe.clear_cache()
					meta = frappe.get_meta(doctype)
			except Exception as e:
				frappe.log_error(
					title="Failed to ensure sync fields locally",
					message=f"Could not ensure sync fields exist locally for {doctype}: {str(e)}"
				)
			
			# Set sync_reference and sync_type on the document before syncing
			try:
				# Reload meta to ensure we have the latest fields
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if has_sync_reference and has_sync_type:
					# Set the fields using db_set to avoid triggering hooks
					frappe.db.set_value(doctype, doc.name, {
						'sync_reference': doc.name,
						'sync_type': 'Local'
					}, update_modified=False)
					frappe.db.commit()
					frappe.logger().info(f"Set sync_reference={doc.name} and sync_type=Local on {doctype} {doc.name}")
			except Exception as e:
				frappe.log_error(
					title="Failed to set sync fields on document",
					message=f"Could not set sync_reference and sync_type on {doctype} {doc.name}: {str(e)}"
				)
		
		# Check internet connection
		has_internet = check_internet_connection(settings)
		
		if has_internet:
			# Try to sync immediately
			try:
				doc_data = prepare_doc_for_sync(doc)
				result = sync_document_to_remote(
				doctype,
				doc.name,
					settings.remote_url,
				settings.admin_api_key,
					api_secret=None,  # Will be decrypted in function
					force_create=True,
					sync_method="Auto",
					settings=settings
				)
				
				if result["status"] == "error":
					# If sync fails, queue it
					queue_sync_job(
						doctype=doctype,
						name=doc.name,
						sync_type="Send",
						document_data=doc_data,
						priority=5
					)
			except:
				# If any error, queue the job
				doc_data = prepare_doc_for_sync(doc)
				queue_sync_job(
					doctype=doctype,
					name=doc.name,
					sync_type="Send",
					document_data=doc_data,
					priority=5
				)
		else:
			# No internet, queue the job
			doc_data = prepare_doc_for_sync(doc)
			queue_sync_job(
				doctype=doctype,
				name=doc.name,
				sync_type="Send",
				document_data=doc_data,
				priority=5
			)
	
	except Exception as e:
		frappe.log_error(
			f"Sync on Create Failed: {doc.doctype} {doc.name}",
			frappe.get_traceback()
		)


def sync_document_on_submit(doc, method: Optional[str] = None):
	"""
	Sync document when it's submitted
	This is called via doc_events hook
	Only works on Local server to send to Remote
	For submittable doctypes, syncs as submitted (docstatus = 1)
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		system_doctypes = [
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings"
		]
		
		if doctype in system_doctypes:
			return
		
		# Only sync submittable doctypes on submit
		if not is_submittable_doctype(doctype):
			return
		
		# Only sync if document is actually submitted (docstatus = 1)
		if doc.docstatus != 1:
			return
		
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return
		
		# Auto-sync doctypes that should always sync (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Only sync if it's in auto-sync doctypes OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# For all syncable and compulsory doctypes, ensure sync fields exist locally and set them
		# This must be done before syncing
		if should_auto_sync or is_enabled_for_send:
			try:
				# Ensure fields exist locally
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if not has_sync_reference or not has_sync_type:
					# Add fields to local doctype
					doctype_doc = frappe.get_doc("DocType", doctype)
					
					if not has_sync_reference:
						doctype_doc.append("fields", {
							"fieldname": "sync_reference",
							"fieldtype": "Data",
							"label": "Sync Reference",
							"description": "Reference to the corresponding document in remote/local instance",
							"read_only": 1,
							"no_copy": 1,
							"unique": 1  # Mark as unique to avoid duplication
						})
					
					if not has_sync_type:
						doctype_doc.append("fields", {
							"fieldname": "sync_type",
							"fieldtype": "Select",
							"label": "Sync Type",
							"options": "Local\nRemote",
							"description": "Indicates whether this document is from Local or Remote instance",
							"read_only": 1,
							"no_copy": 1,
							"default": "Local"
						})
					
					doctype_doc.save(ignore_permissions=True)
					frappe.db.commit()
					frappe.logger().info(f"Created sync_reference and sync_type fields on local doctype {doctype}")
					# Reload meta
					frappe.clear_cache(doctype=doctype)
					frappe.clear_cache()
					frappe.clear_cache(doctype=doctype)
					frappe.clear_cache()
					meta = frappe.get_meta(doctype)
			except Exception as e:
				frappe.log_error(
					title="Failed to ensure sync fields locally",
					message=f"Could not ensure sync fields exist locally for {doctype}: {str(e)}"
				)
			
			# Set sync_reference and sync_type on the document before syncing
			try:
				# Reload meta to ensure we have the latest fields
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if has_sync_reference and has_sync_type:
					# Set the fields using db_set to avoid triggering hooks
					frappe.db.set_value(doctype, doc.name, {
						'sync_reference': doc.name,
						'sync_type': 'Local'
					}, update_modified=False)
					frappe.db.commit()
					frappe.logger().info(f"Set sync_reference={doc.name} and sync_type=Local on {doctype} {doc.name}")
			except Exception as e:
				frappe.log_error(
					title="Failed to set sync fields on document",
					message=f"Could not set sync_reference and sync_type on {doctype} {doc.name}: {str(e)}"
				)
		
		# Check internet connection
		has_internet = check_internet_connection(settings)
		
		if has_internet:
			# Try to sync immediately
			try:
				doc_data = prepare_doc_for_sync(doc)
				result = sync_document_to_remote(
					doctype,
					doc.name,
					settings.remote_url,
					settings.admin_api_key,
					api_secret=None,  # Will be decrypted in function
					force_create=True,
					sync_method="Auto",
					settings=settings
				)
				
				if result["status"] == "error":
					# If sync fails, queue it
					queue_sync_job(
						doctype=doctype,
						name=doc.name,
						sync_type="Send",
						document_data=doc_data,
						priority=5
					)
			except:
				# If any error, queue the job
				doc_data = prepare_doc_for_sync(doc)
				queue_sync_job(
					doctype=doctype,
					name=doc.name,
					sync_type="Send",
					document_data=doc_data,
					priority=5
				)
		else:
			# No internet, queue the job
			doc_data = prepare_doc_for_sync(doc)
			queue_sync_job(
				doctype=doctype,
				name=doc.name,
				sync_type="Send",
				document_data=doc_data,
				priority=5
			)
	
	except Exception as e:
		frappe.log_error(
			f"Sync on Submit Failed: {doc.doctype} {doc.name}",
			frappe.get_traceback()
		)


def sync_document_on_update(doc, method: Optional[str] = None):
	"""
	Sync document when it's updated
	This is called via doc_events hook
	Only works on Local server to send to Remote
	For syncable doctypes with 'send' enabled, auto-syncs on save
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		system_doctypes = [
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings"
		]
		
		if doctype in system_doctypes:
			return
		
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return
		
		# Check if this doctype is enabled for sending
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# For submittable doctypes, only sync submitted documents (docstatus = 1)
		if is_submittable_doctype(doctype):
			if doc.docstatus != 1:
				return  # Skip draft documents
		
		# Auto-sync doctypes that should always sync on update (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		should_auto_sync = doctype in auto_sync_doctypes
		
		# Only sync if it's in auto-sync doctypes OR if it's enabled for sending in syncable doctypes
		# This ensures all doctypes with "send to remote" checked will sync on save
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# Check internet connection
		has_internet = check_internet_connection(settings)
		
		if has_internet:
			# Try to sync immediately
			try:
				doc_data = prepare_doc_for_sync(doc)
				result = sync_document_to_remote(
					doctype,
					doc.name,
					settings.remote_url,
					settings.admin_api_key,
					api_secret=None,  # Will be decrypted in function
					force_create=False,  # Don't force create on update
					sync_method="Auto",
					settings=settings
				)
				
				if result["status"] == "error":
					# If sync fails, queue it
					queue_sync_job(
						doctype=doctype,
						name=doc.name,
						sync_type="Send",
						document_data=doc_data,
						priority=5
					)
			except:
				# If any error, queue the job
				doc_data = prepare_doc_for_sync(doc)
				queue_sync_job(
					doctype=doctype,
					name=doc.name,
					sync_type="Send",
					document_data=doc_data,
					priority=5
				)
		else:
			# No internet, queue the job
			doc_data = prepare_doc_for_sync(doc)
			queue_sync_job(
				doctype=doctype,
				name=doc.name,
				sync_type="Send",
				document_data=doc_data,
				priority=5
			)
	
	except Exception as e:
		frappe.log_error(
			f"Sync on Update Failed: {doc.doctype} {doc.name}",
			frappe.get_traceback()
		)


@frappe.whitelist()
def process_queued_syncs(limit: int = 50):
	"""
	Process queued sync jobs when internet is available
	This is called via cron job
	"""
	try:
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return {
				"status": "error",
				"message": "Havano Sync Settings not properly configured"
			}
		
		# Check internet connection
		has_internet = check_internet_connection(settings)
		
		if not has_internet:
			return {
				"status": "skipped",
				"message": "No internet connection available"
			}
		
		# Get queued jobs, ordered by priority and queued_at
		queued_jobs = frappe.get_all(
			"Havano Sync Queue",
			filters={
				"status": ["in", ["Queued", "Failed"]]
			},
			fields=["name", "sync_type", "doctype", "document_name", "document_data", "retry_count", "max_retries", "next_retry_at"],
			order_by="priority desc, queued_at asc",
			limit=limit * 2  # Get more to filter
		)
		
		# Filter jobs that haven't exceeded max retries and are ready for retry
		now_dt = now_datetime()
		filtered_jobs = []
		for job in queued_jobs:
			# Check retry count
			if job.retry_count >= (job.max_retries or 3):
				continue
			
			# Check if ready for retry (no next_retry_at or it's in the past)
			if job.next_retry_at:
				try:
					next_retry = frappe.utils.get_datetime(job.next_retry_at)
					if next_retry > now_dt:
						continue
				except:
					pass
			
			filtered_jobs.append(job)
			if len(filtered_jobs) >= limit:
				break
		
		queued_jobs = filtered_jobs
		
		if not queued_jobs:
			return {
				"status": "completed",
				"message": "No queued jobs to process",
				"processed": 0
			}
		
		processed = 0
		successful = 0
		failed = 0
		
		for job in queued_jobs:
			try:
				# Update job status to Processing
				queue_doc = frappe.get_doc("Havano Sync Queue", job.name)
				queue_doc.status = "Processing"
				queue_doc.last_attempt_at = now_datetime()
				queue_doc.save(ignore_permissions=True)
				frappe.db.commit()
				
				# Parse document data if available
				doc_data = None
				if job.document_data:
					try:
						doc_data = json.loads(job.document_data)
					except:
						pass
				
				# If no document data, get it from the document
				if not doc_data:
					try:
						doc = frappe.get_doc(job.doctype, job.document_name)
						doc_data = prepare_doc_for_sync(doc)
					except:
						pass
				
				# Try to sync
				result = sync_document_to_remote(
					job.doctype,
					job.document_name,
					settings.remote_url,
				settings.admin_api_key,
					api_secret=None,  # Will be decrypted in function
					force_create=True,
					sync_method="Queue",
					settings=settings
				)
				
				if result["status"] == "success":
					# Mark as completed
					queue_doc.status = "Completed"
					queue_doc.save(ignore_permissions=True)
					successful += 1
				else:
					# Increment retry count
					queue_doc.retry_count += 1
					
					if queue_doc.retry_count >= queue_doc.max_retries:
						queue_doc.status = "Failed"
						queue_doc.error_message = result.get("error", "Max retries reached")
					else:
						queue_doc.status = "Queued"
						queue_doc.next_retry_at = queue_doc.calculate_next_retry()
						queue_doc.error_message = result.get("error", "Sync failed")
					
					queue_doc.save(ignore_permissions=True)
					failed += 1
				
				frappe.db.commit()
				processed += 1
				
			except Exception as e:
				frappe.log_error(
					f"Failed to process queued job {job.name}",
					frappe.get_traceback()
				)
				failed += 1
		
		return {
			"status": "completed",
			"processed": processed,
			"successful": successful,
			"failed": failed
		}
	
	except Exception as e:
		frappe.log_error(
			"Process Queued Syncs Failed",
			frappe.get_traceback()
		)
		return {
			"status": "error",
			"message": str(e)
		}


@frappe.whitelist()
def fetch_document_from_remote(doctype: str, name: str):
	"""
	Fetch a single document from remote server and create/update it locally
	Skips if document already exists locally
	"""
	try:
		settings = get_sync_settings()
		
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			frappe.throw("Havano Sync Settings not properly configured")
		
		# Check if sync is enabled
		if not settings.enable_sync:
			frappe.throw("Sync is disabled in settings")
		
		# Check if doctype is syncable for fetching
		if not should_sync_doctype(doctype, settings, direction="fetch"):
			frappe.throw(f"Doctype {doctype} is not configured for fetching from remote")
		
		# Check if document already exists locally - skip if it does
		# For submittable doctypes, also check by sync_reference
		existing_local_doc = None
		try:
			local_doc = frappe.get_doc(doctype, name)
			existing_local_doc = local_doc
		except frappe.DoesNotExistError:
			# Document doesn't exist by name, check by sync_reference for submittable doctypes
			if is_submittable_doctype(doctype):
				try:
					# Check if a document with this sync_reference already exists
					existing = frappe.get_all(
						doctype,
						filters={"sync_reference": name, "sync_type": "Local"},
						limit=1
					)
					if existing:
						existing_local_doc = frappe.get_doc(doctype, existing[0].name)
						frappe.logger().info(f"Found existing document {doctype} {existing[0].name} by sync_reference {name}")
				except Exception:
					pass
		
		if existing_local_doc:
			return {
				"status": "skipped",
				"message": f"Document {doctype} {existing_local_doc.name} already exists locally (matched by sync_reference {name}). Skipping fetch.",
				"doctype": doctype,
				"name": existing_local_doc.name
			}
		
		# Get decrypted API secret
		api_secret = get_decrypted_api_secret(settings)
		if not api_secret:
			raise ValueError("API Secret is not configured or could not be decrypted")
		
		# Initialize API client
		api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
		
		# Check company filter before fetching
		company = getattr(settings, 'company', None)
		if company:
			# First, get the document to check its company
			try:
				remote_doc_check = api_client.get_document(doctype, name)
				if not belongs_to_company(remote_doc_check, doctype, company):
					return {
						"status": "skipped",
						"message": f"Document {doctype} {name} belongs to different company (not {company}). Skipping fetch.",
						"doctype": doctype,
						"name": name
					}
			except DocumentNotFoundError:
				# Document doesn't exist, will be handled below
				pass
		
		# Sync linked documents first (dependencies) - for fetch direction
		# We need to fetch the document from remote first to see its structure
		# Then sync its linked documents
		remote_doc = None
		fetch_errors = []
		try:
			# First, get the document from remote to see its structure
			remote_doc = api_client.get_document(doctype, name)
			
			# Create a temporary document object to check for links
			# We'll use the remote doc data to create a local doc temporarily
			temp_doc_data = remote_doc.copy()
			temp_doc_data['doctype'] = doctype
			# Remove metadata fields
			metadata_fields = {'name', 'doctype', 'modified', 'modified_by', 'creation', 'owner', '_user_tags', '_comments', '_assign', '_liked_by', '_seen', 'docstatus'}
			temp_doc_data = {k: v for k, v in temp_doc_data.items() if k not in metadata_fields}
			
			# Get meta to check for link fields
			meta = frappe.get_meta(doctype)
			
			# Collect all linked documents that need to be fetched
			links_to_fetch = []
			
			# Check each link field in the remote document
			for field in meta.fields:
				if field.fieldtype == "Link" and field.options:
					link_doctype = field.options
					link_value = temp_doc_data.get(field.fieldname)
					
					if link_value:
						# Check if this linked document exists locally
						try:
							frappe.get_doc(link_doctype, link_value)
							# Document exists locally, no need to fetch
						except frappe.DoesNotExistError:
							# Document doesn't exist locally, add to fetch list
							# Check if it's enabled for fetch OR is a critical dependency
							critical_doctypes = {'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group'}
							should_fetch = should_sync_doctype(link_doctype, settings, direction="fetch") or link_doctype in critical_doctypes
							
							# If company filter is set, check if linked document belongs to that company
							company = getattr(settings, 'company', None)
							if company and should_fetch:
								# For company-specific doctypes, check if they belong to the specified company
								company_doctypes = {'Account', 'Cost Center', 'Warehouse'}
								if link_doctype in company_doctypes:
									# Try to fetch and check company
									try:
										link_doc_check = api_client.get_document(link_doctype, link_value)
										if not belongs_to_company(link_doc_check, link_doctype, company):
											# Skip this linked document as it doesn't belong to the specified company
											frappe.logger().info(f"Skipping linked document {link_doctype} {link_value} - belongs to different company")
											continue
									except Exception as check_error:
										# If we can't check, skip it to be safe
										frappe.logger().warning(f"Could not check company for {link_doctype} {link_value}: {str(check_error)}")
										continue
								elif link_doctype == 'Company':
									# Only fetch the specified company
									if link_value != company:
										frappe.logger().info(f"Skipping linked Company {link_value} - not the specified company {company}")
										continue
							
							if should_fetch:
								links_to_fetch.append((link_doctype, link_value, field.fieldname))
			
			# Also check child table links
			for field in meta.fields:
				if field.fieldtype == "Table" and field.fieldname in temp_doc_data:
					child_table_data = temp_doc_data.get(field.fieldname, [])
					if child_table_data:
						child_meta = frappe.get_meta(field.options)
						for child_row in child_table_data:
							for child_field in child_meta.fields:
								if child_field.fieldtype == "Link" and child_field.options:
									link_doctype = child_field.options
									link_value = child_row.get(child_field.fieldname)
									
									if link_value:
										try:
											frappe.get_doc(link_doctype, link_value)
										except frappe.DoesNotExistError:
											# Check if it's enabled for fetch OR is a critical dependency
											critical_doctypes = {'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group'}
											should_fetch = should_sync_doctype(link_doctype, settings, direction="fetch") or link_doctype in critical_doctypes
											
											# If company filter is set, check if linked document belongs to that company
											company = getattr(settings, 'company', None)
											if company and should_fetch:
												# For company-specific doctypes, check if they belong to the specified company
												company_doctypes = {'Account', 'Cost Center', 'Warehouse'}
												if link_doctype in company_doctypes:
													# Try to fetch and check company
													try:
														link_doc_check = api_client.get_document(link_doctype, link_value)
														if not belongs_to_company(link_doc_check, link_doctype, company):
															# Skip this linked document as it doesn't belong to the specified company
															frappe.logger().info(f"Skipping linked document {link_doctype} {link_value} from child table - belongs to different company")
															continue
													except Exception as check_error:
														# If we can't check, skip it to be safe
														frappe.logger().warning(f"Could not check company for {link_doctype} {link_value}: {str(check_error)}")
														continue
												elif link_doctype == 'Company':
													# Only fetch the specified company
													if link_value != company:
														frappe.logger().info(f"Skipping linked Company {link_value} from child table - not the specified company {company}")
														continue
											
											if should_fetch:
												links_to_fetch.append((link_doctype, link_value, f"{field.fieldname}.{child_field.fieldname}"))
			
			# Now fetch all linked documents
			# Sort by criticality: Account and Cost Center first
			critical_doctypes = {'Account', 'Cost Center'}
			links_to_fetch.sort(key=lambda x: (x[0] not in critical_doctypes, x[0], x[1]))
			
			# Track fetched documents to avoid duplicates and circular dependencies
			fetched_docs = set()
			
			for link_doctype, link_value, field_path in links_to_fetch:
				# Skip if we're trying to fetch the same document we're currently fetching (circular dependency)
				if link_doctype == doctype and link_value == name:
					frappe.logger().warning(f"Skipping circular dependency: {doctype} {name} references itself in {field_path}")
					continue
				
				# Skip if already fetched in this batch
				doc_key = (link_doctype, link_value)
				if doc_key in fetched_docs:
					continue
				try:
					# Check if document already exists (might have been fetched by a previous link)
					try:
						frappe.get_doc(link_doctype, link_value)
						frappe.logger().info(f"Linked document {link_doctype} {link_value} already exists locally")
						continue
					except frappe.DoesNotExistError:
						pass
					
					# Recursively fetch linked documents first
					frappe.logger().info(f"Fetching linked document {link_doctype} {link_value} as dependency for {doctype} {name}")
					fetch_result = fetch_document_from_remote(link_doctype, link_value)
					
					if fetch_result.get("status") == "success" or fetch_result.get("status") == "skipped":
						frappe.logger().info(f"Successfully fetched linked document {link_doctype} {link_value}")
						fetched_docs.add(doc_key)
						# Commit after each successful fetch to ensure it's in the database
						frappe.db.commit()
					else:
						# If fetch failed, add to errors
						error_msg = f"Failed to fetch {link_doctype} {link_value} (referenced in {field_path}): {fetch_result.get('message', 'Unknown error')}"
						fetch_errors.append(error_msg)
						frappe.log_error(
							title="Failed to fetch linked document",
							message=error_msg
						)
						
						# For critical doctypes, verify the document exists locally after fetch attempt
						if link_doctype in critical_doctypes:
							try:
								frappe.get_doc(link_doctype, link_value)
								# Document exists, clear the error
								fetch_errors = [e for e in fetch_errors if not e.startswith(f"Failed to fetch {link_doctype} {link_value}")]
							except frappe.DoesNotExistError:
								# Document still doesn't exist - this is a critical error
								pass
						
				except Exception as fetch_error:
					error_msg = f"Failed to fetch {link_doctype} {link_value} (referenced in {field_path}): {str(fetch_error)}"
					fetch_errors.append(error_msg)
					frappe.log_error(
						title="Failed to fetch linked document",
						message=error_msg
					)
					
					# For critical doctypes, check if document exists despite the error
					if link_doctype in critical_doctypes:
						try:
							frappe.get_doc(link_doctype, link_value)
							# Document exists, clear the error
							fetch_errors = [e for e in fetch_errors if not e.startswith(f"Failed to fetch {link_doctype} {link_value}")]
						except frappe.DoesNotExistError:
							# Document still doesn't exist - this is a critical error
							pass
			
			# If there are critical fetch errors for Company, verify all critical links exist
			if doctype == "Company" and fetch_errors:
				critical_doctypes = {'Account', 'Cost Center'}
				missing_critical = []
				for link_doctype, link_value, field_path in links_to_fetch:
					if link_doctype in critical_doctypes:
						try:
							frappe.get_doc(link_doctype, link_value)
						except frappe.DoesNotExistError:
							missing_critical.append(f"{link_doctype} {link_value} (in {field_path})")
				
				if missing_critical:
					return {
						"status": "error",
						"message": f"Failed to fetch critical linked documents for Company {name}. Missing: {', '.join(missing_critical[:10])}",
						"doctype": doctype,
						"name": name,
						"errors": fetch_errors
					}
		except DocumentNotFoundError:
			# Document doesn't exist on remote, can't fetch linked documents
			return {
				"status": "error",
				"message": f"Document {doctype} {name} not found on remote server",
				"doctype": doctype,
				"name": name
			}
		except Exception as link_error:
			# Log but don't fail - we'll try to fetch the main document anyway
			frappe.log_error(
				title="Error syncing linked documents for fetch",
				message=f"Error syncing linked documents for {doctype} {name}: {str(link_error)}"
			)
		
		# If we haven't fetched the document yet, fetch it now
		if remote_doc is None:
			try:
				remote_doc = api_client.get_document(doctype, name)
			except DocumentNotFoundError:
				return {
					"status": "error",
					"message": f"Document {doctype} {name} not found on remote server",
					"doctype": doctype,
					"name": name
				}
		
		# Fetch document from remote (we already have it, but need to process it)
		start_time = time.time()
		
		# For submittable doctypes, ensure sync fields exist locally
		if is_submittable_doctype(doctype):
			try:
				# Force reload meta to get latest fields
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if not has_sync_reference or not has_sync_type:
					# Add fields to local doctype
					doctype_doc = frappe.get_doc("DocType", doctype)
					
					if not has_sync_reference:
						doctype_doc.append("fields", {
							"fieldname": "sync_reference",
							"fieldtype": "Data",
							"label": "Sync Reference",
							"description": "Reference to the corresponding document in remote/local instance",
							"read_only": 1,
							"no_copy": 1
						})
					
					if not has_sync_type:
						doctype_doc.append("fields", {
							"fieldname": "sync_type",
							"fieldtype": "Select",
							"label": "Sync Type",
							"options": "Local\nRemote",
							"description": "Indicates whether this document is from Local or Remote instance",
							"read_only": 1,
							"no_copy": 1,
							"default": "Local"
						})
					
					doctype_doc.save(ignore_permissions=True)
					frappe.db.commit()
					frappe.logger().info(f"Created sync_reference and sync_type fields on local doctype {doctype}")
					# Reload meta - clear all caches
					frappe.clear_cache(doctype=doctype)
					frappe.clear_cache()
					# Meta will be reloaded on next get_meta call
			except Exception as e:
				frappe.log_error(
					title="Failed to ensure sync fields locally",
					message=f"Could not ensure sync fields exist locally for {doctype}: {str(e)}"
				)
		
		# Create document locally
		# Remove metadata fields that shouldn't be set during creation
		metadata_fields = {'name', 'doctype', 'modified', 'modified_by', 'creation', 'owner', '_user_tags', '_comments', '_assign', '_liked_by', '_seen', 'docstatus'}
		doc_data = {k: v for k, v in remote_doc.items() if k not in metadata_fields}
		doc_data['doctype'] = doctype
		
		# For all syncable and compulsory doctypes, set sync_reference and sync_type
		# Check if doctype is syncable or compulsory
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		is_compulsory = doctype in auto_sync_doctypes
		is_syncable = should_sync_doctype(doctype, settings, direction="send")
		
		if is_compulsory or is_syncable:
			# Set sync_reference to remote document name
			doc_data['sync_reference'] = name
			# Set sync_type to "Remote" (since this is being fetched from remote)
			doc_data['sync_type'] = "Remote"
		
		# Ensure all fetched documents are committed before creating the main document
		frappe.db.commit()
		
		# Create the document
		# First, verify all critical links exist
		local_doc = frappe.get_doc(doc_data)
		
		# For Company, verify all Account and Cost Center links exist before inserting
		if doctype == "Company":
			meta = frappe.get_meta("Company")
			missing_links = []
			for field in meta.fields:
				if field.fieldtype == "Link" and field.options in ("Account", "Cost Center"):
					link_value = doc_data.get(field.fieldname)
					if link_value:
						try:
							# Verify the document exists
							linked_doc = frappe.get_doc(field.options, link_value)
							# Force a refresh to ensure it's in the database
							frappe.db.commit()
						except frappe.DoesNotExistError:
							# Link doesn't exist, try to fetch it one more time
							try:
								frappe.logger().warning(f"Link {field.options} {link_value} not found, attempting to fetch again")
								fetch_result = fetch_document_from_remote(field.options, link_value)
								frappe.db.commit()  # Commit after fetch
								if fetch_result.get("status") != "success" and fetch_result.get("status") != "skipped":
									missing_links.append(f"{field.label}: {link_value}")
							except Exception as e:
								frappe.log_error(
									title="Failed to fetch missing link",
									message=f"Could not fetch {field.options} {link_value} for Company {name}: {str(e)}"
								)
								missing_links.append(f"{field.label}: {link_value}")
			
			if missing_links:
				error_msg = f"Could not find the following required links for Company {name}: {', '.join(missing_links)}"
				frappe.log_error(
					title="Missing links for Company",
					message=error_msg
				)
				return {
					"status": "error",
					"message": error_msg,
					"doctype": doctype,
					"name": name
				}
		
		# Insert the document
		# Use ignore_links=False to ensure all links are validated
		# But we've already verified they exist above
		try:
			local_doc.insert(ignore_permissions=True, ignore_links=False)
			frappe.db.commit()
		except frappe.LinkValidationError as e:
			# If link validation fails, try to handle missing linked documents
			frappe.logger().info(f"LinkValidationError detected for {doctype} {name}, attempting to create missing documents")
			
			# Get API client for handling
			api_secret = get_decrypted_api_secret(settings)
			if api_secret:
				api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
				handled = handle_link_validation_error(
					e, doctype, name, api_client, settings,
					settings.remote_url, settings.admin_api_key, api_secret, direction="fetch"
				)
				
				if handled:
					# Retry the insert operation
					try:
						local_doc.insert(ignore_permissions=True, ignore_links=False)
						frappe.db.commit()
						frappe.logger().info(f"Successfully created {doctype} {name} after handling LinkValidationError")
					except frappe.LinkValidationError as retry_error:
						# If retry still fails, return error
						error_msg = str(retry_error)
						frappe.log_error(
							title="Link validation failed after handling missing documents",
							message=f"Error: {error_msg}"
						)
						return {
							"status": "error",
							"message": f"Link validation failed: {error_msg}",
							"doctype": doctype,
							"name": name
						}
				else:
					# Could not handle the error
					error_msg = str(e)
					return {
						"status": "error",
						"message": f"Link validation failed: {error_msg}",
						"doctype": doctype,
						"name": name
					}
			else:
				# Cannot handle without API credentials
				error_msg = str(e)
				return {
					"status": "error",
					"message": f"Link validation failed: {error_msg}",
					"doctype": doctype,
					"name": name
				}
		
		duration = time.time() - start_time
		
		# Log success
		create_sync_log(
			sync_type="Fetch",
			doctype=doctype,
			document_name=name,
			status="Success",
			message=f"Document fetched successfully from remote instance",
			sync_method="Manual",
			duration_seconds=duration
		)
		
		frappe.logger().info(f"Document {doctype} {name} fetched from remote instance")
		
		return {
			"status": "success",
			"message": f"Document {doctype} {name} fetched successfully",
			"doctype": doctype,
			"name": name
		}
		
	except Exception as e:
		detailed_error = str(e)
		traceback = frappe.get_traceback()
		
		# Log error
		frappe.log_error(
			f"Fetch Failed: {doctype} {name}",
			traceback
		)
		
		create_sync_log(
			sync_type="Fetch",
			doctype=doctype,
			document_name=name,
			status="Failed",
			message=f"Failed to fetch document: {detailed_error}",
			error_details=detailed_error,
			sync_method="Manual"
		)
		
		return {
			"status": "error",
			"message": detailed_error,
			"doctype": doctype,
			"name": name
		}


@frappe.whitelist()
def fetch_all_documents_from_remote(doctype: Optional[str] = None):
	"""
	Fetch all documents from remote server for enabled doctypes
	Skips documents that already exist locally
	"""
	try:
		settings = get_sync_settings()
		
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			frappe.throw("Havano Sync Settings not properly configured")
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return {
				"status": "skipped",
				"message": "Sync is disabled in settings"
			}
		
		# Get decrypted API secret
		api_secret = get_decrypted_api_secret(settings)
		if not api_secret:
			raise ValueError("API Secret is not configured or could not be decrypted")
		
		# Initialize API client
		api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
		
		# Get syncable doctypes with fetch enabled
		syncable_doctypes = get_syncable_doctypes(settings)
		
		results = {
			"success": [],
			"skipped": [],
			"errors": []
		}
		
		for syncable in syncable_doctypes:
			doctype_name = syncable.doctypes
			
			# If specific doctype requested, skip others
			if doctype and doctype_name != doctype:
				continue
			
			# Check if fetch is enabled
			fetch_enabled = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			if not fetch_enabled:
				continue
			
			# Get all documents of this doctype from remote
			try:
				# Use frappe.client.get_list to get all documents
				endpoint = "frappe.client.get_list"
				params = {
					"doctype": doctype_name,
					"limit_page_length": 1000
				}
				
				# For submittable doctypes, only fetch submitted documents (docstatus = 1)
				filters = {}
				if is_submittable_doctype(doctype_name):
					filters["docstatus"] = 1
				
				# Add company filter if specified in settings
				company = getattr(settings, 'company', None)
				if company:
					# For doctypes that have company field, filter by company
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
					if doctype_name in company_doctypes:
						filters["company"] = company
					elif doctype_name == 'Company':
						# For Company doctype, only fetch the specified company
						filters["name"] = company
				
				# Apply filters if any
				if filters:
					params["filters"] = json.dumps(filters)
				
				remote_docs = api_client._make_request("GET", endpoint, params=params)
				
				if not remote_docs or not isinstance(remote_docs, list):
					continue
				
				for remote_doc_info in remote_docs:
					doc_name = remote_doc_info.get('name')
					if not doc_name:
						continue
					
					# Check if document already exists locally - skip if it does
					try:
						frappe.get_doc(doctype_name, doc_name)
						results["skipped"].append({
							"doctype": doctype_name,
							"name": doc_name,
							"message": "Document already exists locally"
						})
						continue
					except frappe.DoesNotExistError:
						# Document doesn't exist locally, fetch it
						pass
					
					# Fetch the document (call the function directly, not via API)
					try:
						# Get decrypted API secret
						api_secret = get_decrypted_api_secret(settings)
						
						# Fetch document from remote
						remote_doc = api_client.get_document(doctype_name, doc_name)
						
						# Check if document belongs to specified company
						company = getattr(settings, 'company', None)
						if company and not belongs_to_company(remote_doc, doctype_name, company):
							results["skipped"].append({
								"doctype": doctype_name,
								"name": doc_name,
								"message": f"Document belongs to different company (not {company})"
							})
							continue
						
						# Remove metadata fields that shouldn't be set during creation
						metadata_fields = {'name', 'doctype', 'modified', 'modified_by', 'creation', 'owner', '_user_tags', '_comments', '_assign', '_liked_by', '_seen', 'docstatus'}
						doc_data = {k: v for k, v in remote_doc.items() if k not in metadata_fields}
						doc_data['doctype'] = doctype_name
						
						# For all syncable and compulsory doctypes, ensure sync fields exist locally and set them
						# Check if doctype is syncable or compulsory
						auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
						is_compulsory = doctype_name in auto_sync_doctypes
						is_syncable = should_sync_doctype(doctype_name, settings, direction="send")
						
						if is_compulsory or is_syncable:
							try:
								# Force reload meta to get latest fields
								frappe.clear_cache(doctype=doctype_name)
								frappe.clear_cache()
								meta = frappe.get_meta(doctype_name)
								has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
								has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
								
								if not has_sync_reference or not has_sync_type:
									# Add fields to local doctype
									doctype_doc = frappe.get_doc("DocType", doctype_name)
									
									if not has_sync_reference:
										doctype_doc.append("fields", {
											"fieldname": "sync_reference",
											"fieldtype": "Data",
											"label": "Sync Reference",
											"description": "Reference to the corresponding document in remote/local instance",
											"read_only": 1,
											"no_copy": 1,
											"unique": 1  # Mark as unique to avoid duplication
										})
									
									if not has_sync_type:
										doctype_doc.append("fields", {
											"fieldname": "sync_type",
											"fieldtype": "Select",
											"label": "Sync Type",
											"options": "Local\nRemote",
											"description": "Indicates whether this document is from Local or Remote instance",
											"read_only": 1,
											"no_copy": 1,
											"default": "Local"
										})
									
									doctype_doc.save(ignore_permissions=True)
									frappe.db.commit()
									frappe.logger().info(f"Created sync_reference and sync_type fields on local doctype {doctype_name}")
									# Reload meta - clear all caches
									frappe.clear_cache(doctype=doctype_name)
									frappe.clear_cache()
									# Meta will be reloaded on next get_meta call
							except Exception as e:
								frappe.log_error(
									title="Failed to ensure sync fields locally",
									message=f"Could not ensure sync fields exist locally for {doctype_name}: {str(e)}"
								)
							
							# Set sync_reference and sync_type
							doc_data['sync_reference'] = doc_name
							doc_data['sync_type'] = "Remote"
						
						# Create the document
						local_doc = frappe.get_doc(doc_data)
						local_doc.insert(ignore_permissions=True)
						frappe.db.commit()
						
						results["success"].append({
							"doctype": doctype_name,
							"name": doc_name,
							"message": "Document fetched successfully"
						})
					except DocumentNotFoundError:
						results["errors"].append({
							"doctype": doctype_name,
							"name": doc_name,
							"message": "Document not found on remote server"
						})
					except Exception as fetch_error:
						results["errors"].append({
							"doctype": doctype_name,
							"name": doc_name,
							"message": f"Error fetching document: {str(fetch_error)}"
						})
						
			except Exception as e:
				results["errors"].append({
					"doctype": doctype_name,
					"message": f"Error fetching documents: {str(e)}"
				})
		
		return {
			"status": "completed",
			"message": f"Fetch completed. Success: {len(results['success'])}, Skipped: {len(results['skipped'])}, Errors: {len(results['errors'])}",
			"results": results
		}
		
	except Exception as e:
		frappe.log_error(
			title="Fetch All Failed",
			message=frappe.get_traceback()
		)
		return {
			"status": "error",
			"message": str(e)
		}


@frappe.whitelist()
def sync_all_pending_documents(doctype: Optional[str] = None):
	"""
	Sync all pending documents (for cron job or manual trigger)
	If doctype is provided, only sync that doctype
	"""
	try:
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			frappe.throw("Havano Sync Settings not properly configured")
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return {
				"status": "skipped",
				"message": "Sync is disabled in settings"
			}
		
		# Check internet connection
		has_internet = check_internet_connection(settings)
		
		if not has_internet:
			# Queue all documents instead
			syncable_doctypes = get_syncable_doctypes(settings)
			queued_count = 0
			
			for syncable in syncable_doctypes:
				doctype_name = syncable.doctypes
				
				if doctype and doctype_name != doctype:
					continue
				
				# Check if send is enabled (for syncing to remote)
				send_enabled = cint(syncable.get('send', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'send', 0))
				if not send_enabled:
					continue
				
				# For submittable doctypes, only sync submitted documents (docstatus = 1)
				filters = {}
				if is_submittable_doctype(doctype_name):
					filters["docstatus"] = 1
				
				docs = frappe.get_all(
					doctype_name,
					filters=filters,
					fields=["name"],
					limit=1000
				)
				
				for doc_info in docs:
					try:
						doc = frappe.get_doc(doctype_name, doc_info.name)
						doc_data = prepare_doc_for_sync(doc)
						queue_sync_job(
							doctype=doctype_name,
							name=doc_info.name,
							sync_type="Send",
							document_data=doc_data,
							priority=3
						)
						queued_count += 1
					except:
						pass
			
			return {
				"status": "queued",
				"message": f"No internet connection. {queued_count} documents queued for sync.",
				"queued_count": queued_count
			}
		
		syncable_doctypes = get_syncable_doctypes(settings)
		
		results = {
			"success": [],
			"errors": []
		}
		
		for syncable in syncable_doctypes:
			doctype_name = syncable.doctypes
			
			# If specific doctype requested, skip others
			if doctype and doctype_name != doctype:
				continue
			
			# Check if send is enabled (for syncing to remote)
			if not (syncable.get('send', 0) or syncable.get('send', False)):
				continue
			
			# Get all documents of this doctype
			# For submittable doctypes, only sync submitted documents (docstatus = 1)
			filters = {}
			if is_submittable_doctype(doctype_name):
				filters["docstatus"] = 1
			
			docs = frappe.get_all(
				doctype_name,
				filters=filters,
				fields=["name"],
				limit=1000  # Limit to prevent timeout
			)
			
			for doc_info in docs:
				result = sync_document_to_remote(
					doctype_name,
					doc_info.name,
					settings.remote_url,
					settings.admin_api_key,
					api_secret=None,  # Will be decrypted in function
					sync_method="Cron",
					settings=settings
				)
				
				if result["status"] == "success":
					results["success"].append(result)
				else:
					results["errors"].append(result)
			
			# After all sends for this doctype are complete, check if fetch is also enabled
			# Only trigger fetch once per doctype, not after each document
			fetch_enabled = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			if fetch_enabled and len(results["success"]) > 0:
				try:
					# Trigger fetch for this doctype (will skip documents that already exist locally)
					frappe.logger().info(f"Fetch is enabled for {doctype_name}. Triggering fetch after successful sends.")
					# Use enqueue to avoid blocking
					frappe.enqueue(
						"havano_sync.havano_sync.tasks.sync.fetch_all_documents_from_remote",
						doctype=doctype_name,
						queue="default",
						timeout=300,
						is_async=True
					)
				except Exception as fetch_error:
					# Log but don't fail the sync operation
					frappe.log_error(
						title="Failed to trigger fetch after sends",
						message=f"Error triggering fetch for {doctype_name} after successful sends: {str(fetch_error)}"
					)
					# Queue failed syncs
					try:
						doc = frappe.get_doc(doctype_name, doc_info.name)
						doc_data = prepare_doc_for_sync(doc)
						queue_sync_job(
							doctype=doctype_name,
							name=doc_info.name,
							sync_type="Send",
							document_data=doc_data,
							priority=3
						)
					except:
						pass
		
		return {
			"status": "completed",
			"total_synced": len(results["success"]),
			"total_errors": len(results["errors"]),
			"results": results
		}
	
	except Exception as e:
		frappe.log_error(
			"Sync All Documents Failed",
			frappe.get_traceback()
		)
		frappe.throw(f"Sync failed: {str(e)}")


@frappe.whitelist()
def sync_single_document(doctype: str, name: str):
	"""
	Manually sync a single document
	"""
	try:
		settings = get_sync_settings()
		
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			frappe.throw("Havano Sync Settings not properly configured")
		
		# Check if sync is enabled
		if not settings.enable_sync:
			frappe.throw("Sync is disabled in settings")
		
		# Check if doctype is syncable for sending (optional - allow sync even if not explicitly enabled)
		# This allows manual syncs even if "send" is not checked
		# The should_sync_doctype check is now informational rather than restrictive
		
		# Check internet connection
		has_internet = check_internet_connection(settings)
		
		if not has_internet:
			# Queue the document
			try:
				doc = frappe.get_doc(doctype, name)
				doc_data = prepare_doc_for_sync(doc)
				queue_id = queue_sync_job(
					doctype=doctype,
					name=name,
					sync_type="Send",
					document_data=doc_data,
					priority=10  # High priority for manual sync
				)
				return {
					"status": "queued",
					"message": "No internet connection. Document queued for sync.",
					"queue_id": queue_id
				}
			except Exception as e:
				frappe.throw(f"Failed to queue document: {str(e)}")
		
		result = sync_document_to_remote(
			doctype,
			name,
			settings.remote_url,
			settings.admin_api_key,
			api_secret=None,  # Will be decrypted in function
			sync_method="Manual",
			settings=settings
		)
		
		# If sync fails, queue it
		if result["status"] == "error":
			try:
				doc = frappe.get_doc(doctype, name)
				doc_data = prepare_doc_for_sync(doc)
				queue_id = queue_sync_job(
					doctype=doctype,
					name=name,
					sync_type="Send",
					document_data=doc_data,
					priority=10
				)
				result["queue_id"] = queue_id
				result["message"] = "Sync failed. Document queued for retry."
			except:
				pass
		
		return result
	
	except Exception as e:
		frappe.log_error(
			f"Manual Sync Failed: {doctype} {name}",
			frappe.get_traceback()
		)
		frappe.throw(f"Sync failed: {str(e)}")


def sync_cron_job():
	"""
	Cron job function to sync all pending documents
	This is called via scheduler_events hook
	"""
	try:
		sync_all_pending_documents()
	except Exception as e:
		frappe.log_error(
			"Cron Sync Job Failed",
			frappe.get_traceback()
		)


def process_queue_cron_job():
	"""
	Cron job function to process queued syncs
	This is called via scheduler_events hook
	"""
	try:
		process_queued_syncs(limit=50)
	except Exception as e:
		frappe.log_error(
			"Process Queue Cron Job Failed",
			frappe.get_traceback()
		)
