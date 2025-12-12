# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
import time
import requests
from typing import Optional, Dict, Any, TYPE_CHECKING
from frappe.utils import cint
from havano_sync.havano_sync.utils.sync_api import SyncAPI, DocumentNotFoundError, DuplicateEntryError
from havano_sync.havano_sync.tasks.utils import (
    get_sync_settings,
    should_sync_doctype,
    is_submittable_doctype,
    get_decrypted_api_secret,
    belongs_to_company
)
from havano_sync.havano_sync.tasks.document_preparation import prepare_doc_for_sync, create_minimal_master_document
from havano_sync.havano_sync.tasks.queue import create_sync_log

if TYPE_CHECKING:
    from frappe.model.document import Document


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
				"Failed to ensure sync fields on remote",
				f"Could not ensure sync fields exist on remote doctype {doctype}: {str(e)}"
			)
			return False
			
	except Exception as e:
		frappe.log_error(
			"Failed to check/create sync fields",
			f"Error checking/creating sync fields for {doctype}: {str(e)}"
		)
		return False




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
				"Failed to get meta for LinkValidationError handling",
				f"Could not get meta for {doctype}: {str(error)}"
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
		frappe.log_error(
			"Could not parse missing documents from LinkValidationError",
			f"Could not parse missing documents from LinkValidationError: {error_msg}"
		)
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
						"Failed to sync missing document to remote",
						f"Could not sync {link_doctype} {link_name} to remote: {str(sync_error)}"
					)
			else:  # fetch direction - document already exists locally
				doc_created = True
				created_count += 1
				frappe.logger().info(f"Missing document {link_doctype} {link_name} already exists locally")
		except frappe.DoesNotExistError:
			# Document doesn't exist locally, try to fetch from remote
			if direction == "fetch":
				try:
					# Import here to avoid circular dependency
					from havano_sync.havano_sync.tasks.fetch_operations import fetch_document_from_remote
					fetch_result = fetch_document_from_remote(link_doctype, link_name)
					if fetch_result.get("status") == "success":
						doc_created = True
						created_count += 1
						frappe.logger().info(f"Created missing document {link_doctype} {link_name} locally from remote")
				except Exception as fetch_error:
					frappe.log_error(
						"Failed to fetch missing document from remote",
						f"Could not fetch {link_doctype} {link_name} from remote: {str(fetch_error)}"
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
									"Failed to sync minimal master document to remote",
									f"Could not sync minimal {link_doctype} {link_name} to remote: {str(sync_error)}"
								)
					except Exception as create_error:
						frappe.log_error(
							"Failed to create minimal master document",
							f"Could not create minimal {link_doctype} {link_name}: {str(create_error)}"
						)
				else:
					frappe.log_error(
						"Missing document not found",
						f"Missing document {link_doctype} {link_name} does not exist locally or on remote. Cannot create."
					)
		
		if not doc_created:
			frappe.log_error(
				f"Could not create missing document {link_doctype} {link_name}",
				f"Could not create missing document {link_doctype} {link_name}"
			)
	
	if created_count > 0:
		frappe.logger().info(f"Created {created_count} missing document(s) for {doctype} {name}. Operation should be retried.")
		return True
	
	return False




def resolve_remote_document_name_by_sync_reference(
	api_client: Any,
	doctype: str,
	local_name: str
) -> Optional[str]:
	"""
	Resolve the remote document name using sync_reference field.
	This is the preferred method for finding remote documents when local names have -Local suffix.
	
	Args:
		api_client: SyncAPI client instance
		doctype: Document type
		local_name: Local document name (may have -Local suffix)
	
	Returns:
		Remote document name if found, None otherwise
	"""
	try:
		# Use custom API endpoint to find document by sync_reference
		# This works around the limitation that frappe.client.get_list doesn't allow custom fields in filters
		remote_name = api_client.find_document_by_sync_reference(doctype, local_name, sync_type="Local")
		if remote_name:
			frappe.logger().debug(f"Resolved remote name for {doctype} {local_name} -> {remote_name} using sync_reference")
			return remote_name
		
		# Fallback: try to get the document by name directly (in case names match)
		try:
			remote_doc = api_client.get_document(doctype, local_name)
			# Document exists with same name, check if sync_reference matches
			if remote_doc.get('sync_reference') == local_name and remote_doc.get('sync_type') == 'Local':
				frappe.logger().debug(f"Resolved remote name for {doctype} {local_name} -> {local_name} (name matches)")
				return local_name
		except (DocumentNotFoundError, requests.exceptions.HTTPError):
			# Document doesn't exist with that name
			pass
		except Exception as e:
			frappe.logger().debug(f"Error checking document by name for {doctype} {local_name}: {str(e)}")
	except Exception as e:
		frappe.logger().debug(f"Could not resolve remote name by sync_reference for {doctype} {local_name}: {str(e)}")
	
	return None


def sync_linked_documents(
	doc: "Document",
	api_client: Any,
	settings: Any,
	target_url: str,
	api_key: str,
	api_secret: str,
	synced_docs: Optional[set] = None,
	direction: str = "send",
	link_field_mapping: Optional[dict] = None
) -> dict:
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
		link_field_mapping: Dictionary to store mappings of (doctype, local_name) -> remote_name
	
	Returns:
		Dictionary mapping (doctype, local_name) -> remote_name for all synced linked documents
	"""
	if synced_docs is None:
		synced_docs = set()
	if link_field_mapping is None:
		link_field_mapping = {}
	
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
					# Remove -Local suffix if present (doctype names should never have -Local suffix)
					if doctype_name.endswith("-Local"):
						original_doctype = doctype_name
						doctype_name = doctype_name[:-6]  # Remove "-Local" (6 characters)
						frappe.log_error(
						f"Found doctype name with -Local suffix: {original_doctype}",
						f"Found doctype name with -Local suffix in syncable doctypes: {original_doctype}. Using {doctype_name} instead."
					)
					enabled_doctypes.add(doctype_name)
	
	# Track link fields in the document and their remote names
	link_fields = []
	link_field_mapping = {}  # Maps (doctype, local_name) -> remote_name
	
	# Get link fields from meta
	for field in doc.meta.fields:
		if field.fieldtype == "Link" and field.options:
			link_doctype = field.options
			link_value = doc.get(field.fieldname)
			
			# Skip None, null, empty string, or the string "None"
			if not link_value or link_value in (None, "", "None", "null"):
				continue
			
			# Exempted doctypes that should never be synced (auto-generated, ledger entries, etc.)
			exempted_doctypes = {
				'GL Entry', 'Stock Ledger Entry', 'Payment Ledger Entry', 'Repost Payment Ledger',
				'User', 'Error Log', 'Activity Log', 'Comment', 'Version', 'Communication',
				'Email Queue', 'Email Queue Recipient', 'Notification Log',
				'Scheduled Job Log', 'Scheduled Job Type', 'DocType',
				'Route History', 'Webform', 'Access Log', 'Portal Settings'
			}
			
			# Skip exempted doctypes
			if link_doctype in exempted_doctypes:
				continue
			
			# Sync linked documents if they exist and are either enabled OR are critical dependencies
			# Critical dependencies include: Account, Cost Center, Warehouse, Customer, Supplier, etc.
			critical_doctypes = {
				'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group',
				'Customer', 'Supplier', 'Item', 'Price List', 'Territory', 'Sales Person'
			}
			should_sync = link_value and (link_doctype in enabled_doctypes or link_doctype in critical_doctypes)
			
			if should_sync:
				# Check if this linked document exists on remote
				# Use sync_reference to find the remote document name (preferred method)
				exists_on_remote = False
				remote_name = link_value  # Default to local name
				
				# First, try to resolve using sync_reference (most reliable)
				resolved_remote_name = resolve_remote_document_name_by_sync_reference(
					api_client, link_doctype, link_value
				)
				if resolved_remote_name:
					exists_on_remote = True
					remote_name = resolved_remote_name
					frappe.logger().debug(f"Linked document {link_doctype} {link_value} resolved to remote name {remote_name} via sync_reference")
			else:
				# Fallback: Try checking with the local name directly
				try:
					api_client.get_document(link_doctype, link_value)
					# Document exists with same name
					exists_on_remote = True
					remote_name = link_value
					frappe.logger().debug(f"Linked document {link_doctype} {link_value} exists on remote with same name")
				except (DocumentNotFoundError, requests.exceptions.HTTPError):
					# Document doesn't exist - will need to sync it
					exists_on_remote = False
				except Exception as e:
					# Other error occurred - log but continue
					frappe.log_error(
						f"Error checking if {link_doctype} {link_value} exists on remote",
						f"Error checking if {link_doctype} {link_value} exists on remote: {str(e)}"
					)
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
								child_mapping = sync_linked_documents(
									linked_doc, api_client, settings, 
									target_url, api_key, api_secret, synced_docs, direction=direction,
									link_field_mapping=link_field_mapping
								) or {}
								link_field_mapping.update(child_mapping)
								
								# Now sync the linked document itself to remote
								if direction == "send":
									sync_result = sync_document_to_remote(
										link_doctype, link_value, target_url, 
										api_key, api_secret, force_create=True,
										sync_method="Auto", settings=settings
									)
									# Get the remote name from sync result or resolve using sync_reference
									if sync_result and sync_result.get("status") == "success":
										# Try to resolve the actual remote name using sync_reference (most reliable)
										remote_doc_name = resolve_remote_document_name_by_sync_reference(
											api_client, link_doctype, link_value
										) or sync_result.get("name") or link_value
										# Store the mapping for updating document data later
										link_field_mapping[(link_doctype, link_value)] = remote_doc_name
										frappe.logger().info(f"Successfully synced linked document {link_doctype} {link_value} to remote as {remote_doc_name}")
									else:
										# If sync failed, try to resolve existing remote name, otherwise use local name
										remote_doc_name = resolve_remote_document_name_by_sync_reference(
											api_client, link_doctype, link_value
										) or link_value
										link_field_mapping[(link_doctype, link_value)] = remote_doc_name
										frappe.log_error(
										f"Linked document {link_doctype} {link_value} sync may have failed",
										f"Linked document {link_doctype} {link_value} sync may have failed, using resolved name {remote_doc_name}"
									)
								else:  # fetch direction - but document exists locally, so no need to fetch
									frappe.logger().info(f"Linked document {link_doctype} {link_value} exists locally, skipping fetch")
							except Exception as sync_error:
								frappe.log_error(
									"Failed to sync linked document",
									f"Could not sync linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name}: {str(sync_error)}"
								)
						else:
							# Document doesn't exist locally either
							# For fetch direction, try to fetch it from remote
							if direction == "fetch":
								try:
									# Import here to avoid circular dependency
									from havano_sync.havano_sync.tasks.fetch_operations import fetch_document_from_remote
									frappe.logger().info(f"Fetching linked document {link_doctype} {link_value} from remote as dependency for {doc.doctype} {doc.name}")
									fetch_result = fetch_document_from_remote(link_doctype, link_value)
									if fetch_result.get("status") == "success":
										frappe.logger().info(f"Successfully fetched linked document {link_doctype} {link_value} from remote")
									else:
										frappe.log_error(
											"Failed to fetch linked document from remote",
											f"Could not fetch linked document {link_doctype} {link_value} from remote: {fetch_result.get('message', 'Unknown error')}"
										)
								except Exception as fetch_error:
									frappe.log_error(
										"Failed to fetch linked document from remote",
										f"Could not fetch linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name}: {str(fetch_error)}"
									)
							else:  # send direction
								# Document doesn't exist locally or on remote - can't sync it
								frappe.log_error(
									"Linked document not found",
									f"Linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name} does not exist locally or on remote. Cannot sync."
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
							
							# Skip None, null, empty string, or the string "None"
							if not link_value or link_value in (None, "", "None", "null"):
								continue
							
							# Exempted doctypes that should never be synced (auto-generated, ledger entries, etc.)
							exempted_doctypes = {
								'GL Entry', 'Stock Ledger Entry', 'Payment Ledger Entry', 'Repost Payment Ledger',
								'User', 'Error Log', 'Activity Log', 'Comment', 'Version', 'Communication',
								'Email Queue', 'Email Queue Recipient', 'Notification Log',
								'Scheduled Job Log', 'Scheduled Job Type', 'DocType',
								'Route History', 'Webform', 'Access Log', 'Portal Settings'
							}
							
							# Skip exempted doctypes
							if link_doctype in exempted_doctypes:
								continue
							
							# Sync linked documents if they exist and are either enabled OR are critical dependencies
							critical_doctypes = {
								'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group',
								'Customer', 'Supplier', 'Item', 'Price List', 'Territory', 'Sales Person'
							}
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
									frappe.log_error(
										f"Error checking if {link_doctype} {link_value} exists on remote",
										f"Error checking if {link_doctype} {link_value} exists on remote: {str(e)}"
									)
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
												child_mapping = sync_linked_documents(
													linked_doc, api_client, settings,
													target_url, api_key, api_secret, synced_docs, direction=direction,
													link_field_mapping=link_field_mapping
												) or {}
												link_field_mapping.update(child_mapping)
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
													"Failed to sync linked document from child table",
													f"Could not sync linked document {link_doctype} {link_value} from {field.fieldname} in {doc.doctype} {doc.name}: {str(sync_error)}"
												)
										else:
											# Document doesn't exist locally either
											if direction == "fetch":
												try:
													# Import here to avoid circular dependency
													from havano_sync.havano_sync.tasks.fetch_operations import fetch_document_from_remote
													frappe.logger().info(f"Fetching linked document {link_doctype} {link_value} from child table from remote")
													fetch_result = fetch_document_from_remote(link_doctype, link_value)
													if fetch_result.get("status") == "success":
														frappe.logger().info(f"Successfully fetched linked document {link_doctype} {link_value} from child table")
												except Exception as fetch_error:
													frappe.log_error(
														"Failed to fetch linked document from child table",
														f"Could not fetch linked document {link_doctype} {link_value} from {field.fieldname}: {str(fetch_error)}"
													)
											else:  # send direction
												frappe.log_error(
													"Linked document from child table not found",
													f"Linked document {link_doctype} {link_value} from {field.fieldname} in {doc.doctype} {doc.name} does not exist locally or on remote"
												)
	
	# Return the mapping of local names to remote names
	return link_field_mapping




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
		# Exempted doctypes that should never be synced (auto-generated, ledger entries, etc.)
		exempted_doctypes = {
			'User', 'GL Entry', 'Stock Ledger Entry', 'Payment Ledger Entry', 'Repost Payment Ledger',
			'Error Log', 'Activity Log', 'Comment', 'Version', 'Communication',
			'Email Queue', 'Email Queue Recipient', 'Notification Log',
			'Scheduled Job Log', 'Scheduled Job Type', 'DocType',
			'Route History', 'Webform', 'Access Log', 'Portal Settings'
		}
		
		if doctype in exempted_doctypes:
			return {
				"status": "skipped",
				"message": f"{doctype} doctype is exempted from syncing",
				"doctype": doctype,
				"name": name
			}
		
		# Check sync_status - skip if already synced or fetched (avoid duplicates)
		if settings:
			from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists, has_sync_status, should_sync_doctype
			# Ensure field exists for all syncable doctypes
			if should_sync_doctype(doctype, settings, direction="send"):
				ensure_sync_status_field_exists(doctype)
				# Check if already synced or fetched
				if has_sync_status(doctype, name):
					sync_status = frappe.db.get_value(doctype, name, 'sync_status')
					return {
						"status": "skipped",
						"message": f"Document {doctype} {name} already has sync_status='{sync_status}', skipping to avoid duplicate",
						"doctype": doctype,
						"name": name
					}
		
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
		# Handle case where document might have been renamed with -Local suffix (for non-Sales Invoice/Payment Entry)
		# For Sales Invoice and Payment Entry with naming series, ensure we use the renamed name
		actual_name = name
		if doctype in ("Sales Invoice", "Payment Entry"):
			# For Sales Invoice and Payment Entry, check if naming series is configured
			# If so, the document should have been renamed with the naming series
			# We should only sync if the document has been renamed (name matches naming series pattern)
			if settings:
				naming_series_to_check = None
				if doctype == "Payment Entry" and hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series:
					naming_series_to_check = settings.payment_entry_naming_series
				elif doctype == "Sales Invoice" and hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series:
					naming_series_to_check = settings.sales_invoice_naming_series
				
				if naming_series_to_check:
					# Check if document exists with current name
					if not frappe.db.exists(doctype, name):
						# Document doesn't exist with this name - might have been renamed
						# Try to find the renamed document by searching for recent documents with naming series pattern
						import re
						# Extract pattern from naming series (e.g., "ACC-SINV-.YYYY.-" -> "ACC-SINV-")
						# or "ACC-SINV-STORE1-.YYYY.-" -> "ACC-SINV-STORE1-"
						pattern_match = re.match(r'^([A-Z0-9\-]+)', naming_series_to_check)
						if pattern_match:
							prefix = pattern_match.group(1).rstrip('-')
							# Find documents with this prefix that were created recently and have sync_type = Local
							table_name = f"tab{doctype}"
							recent_docs = frappe.db.sql(f"""
								SELECT name FROM `{table_name}`
								WHERE name LIKE %s
								AND name != %s
								AND sync_type = 'Local'
								AND creation >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
								ORDER BY creation DESC
								LIMIT 1
							""", (prefix + '%', name), as_dict=True)
							
							if recent_docs:
								actual_name = recent_docs[0].name
								# Verify the renamed document has sync_type="Local" before using it
								if frappe.db.has_column(doctype, 'sync_type'):
									renamed_sync_type = frappe.db.get_value(doctype, actual_name, 'sync_type')
									if renamed_sync_type == "Remote":
										# frappe.log_error(
										# 	f"[SYNC] Found renamed document {doctype} {actual_name} but sync_type='Remote'",
										# 	f"[SYNC] Found renamed document {doctype} {actual_name} but it has sync_type='Remote'. Skipping sync."
										# )
										return {
											"status": "skipped",
											"doctype": doctype,
											"name": name,
											"message": f"Document {doctype} {name} was renamed to {actual_name} but it has sync_type='Remote' and should not be synced to remote"
										}
								# frappe.log_error(
								# 	f"[SYNC] Found renamed document {doctype} {actual_name}",
								# 	f"Found renamed document {doctype} {actual_name} by pattern matching (was {name})"
								# )
							else:
								# Could not find renamed document - skip sync
								# frappe.log_error(
								# 	f"[SYNC] Document {doctype} {name} not found",
								# 	f"[SYNC] Document {doctype} {name} not found and could not find renamed version (no recent documents with pattern {prefix}%)"
								# )
								return {
									"status": "skipped",
									"doctype": doctype,
									"name": name,
									"message": f"Document {doctype} {name} not found. It may have been renamed with naming series. Sync will happen after rename completes."
								}
						else:
							# Could not parse naming series pattern
							# frappe.log_error(
							# 	f"[SYNC] Document {doctype} {name} not found - could not parse naming series",
							# 	f"[SYNC] Document {doctype} {name} not found and could not parse naming series pattern"
							# )
							return {
								"status": "skipped",
								"doctype": doctype,
								"name": name,
								"message": f"Document {doctype} {name} not found. It may have been renamed with naming series. Sync will happen after rename completes."
							}
					else:
						# Document exists - check if the name matches the expected naming series pattern
						# Extract prefix from naming series (e.g., "ACC-SINV-STORE2-.YYYY.-" -> "ACC-SINV-STORE2-")
						import re
						pattern_match = re.match(r'^([A-Z0-9\-]+)', naming_series_to_check)
						if pattern_match:
							expected_prefix = pattern_match.group(1).rstrip('-')
							# Check if document name starts with the expected prefix
							if name.startswith(expected_prefix):
								# Name matches the expected pattern - proceed with sync
								actual_name = name
								# frappe.log_error(
								# 	f"[SYNC] Document {doctype} {name} name matches expected naming series pattern {expected_prefix}",
								# 	f"[SYNC] Document {doctype} {name} name matches expected naming series pattern. Proceeding with sync."
								# )
							else:
								# Name doesn't match pattern - document hasn't been renamed yet
								doc_check = frappe.get_doc(doctype, name)
								current_naming_series = doc_check.get('naming_series', '')
								# frappe.log_error(
								# 	f"[SYNC] Document {doctype} {name} name does not match expected naming series pattern",
								# 	f"[SYNC] Document {doctype} {name} has naming series {current_naming_series} but expected {naming_series_to_check}. Name doesn't match pattern {expected_prefix}. Skipping sync - rename should queue sync."
								# )
								return {
									"status": "skipped",
									"doctype": doctype,
									"name": name,
									"message": f"Document {doctype} {name} has not been renamed with naming series yet. Sync will happen after rename completes."
								}
						else:
							# Could not parse naming series pattern - check naming_series field as fallback
							doc_check = frappe.get_doc(doctype, name)
							current_naming_series = doc_check.get('naming_series', '')
							if current_naming_series != naming_series_to_check:
								# frappe.log_error(
								# 	f"[SYNC] Document {doctype} {name} naming_series field does not match",
								# 	f"[SYNC] Document {doctype} {name} has naming series {current_naming_series} but expected {naming_series_to_check}. Skipping sync - rename should queue sync."
								# )
								return {
									"status": "skipped",
									"doctype": doctype,
									"name": name,
									"message": f"Document {doctype} {name} has not been renamed with naming series yet. Sync will happen after rename completes."
								}
							actual_name = name
		else:
			# For other doctypes, check for -Local suffix
			if not frappe.db.exists(doctype, name):
				if not name.endswith("-Local"):
					local_name = f"{name}-Local"
					if frappe.db.exists(doctype, local_name):
						actual_name = local_name
						# frappe.log_error(
						# 	f"Document {doctype} {name} not found, using renamed name {actual_name}",
						# 	f"Document {doctype} {name} not found, using renamed name {actual_name}"
						# )
		
		try:
			doc = frappe.get_doc(doctype, actual_name)
		except Exception as get_doc_error:
			# frappe.log_error(
			# 	f"[SYNC] Failed to get document {doctype} {actual_name}",
			# 	f"[SYNC] Failed to get document {doctype} {actual_name}: {str(get_doc_error)}\nTraceback: {frappe.get_traceback()}"
			# )
			raise
		
		# CRITICAL: Never sync documents with sync_type="Remote" to remote
		# These documents came from remote, so they should not be synced back
		doc_sync_type = getattr(doc, 'sync_type', None)
		if doc_sync_type == "Remote":
			# frappe.log_error(
			# 	f"[SYNC] Skipping sync for {doctype} {actual_name} - sync_type is 'Remote'",
			# 	f"[SYNC] Skipping sync for {doctype} {actual_name} - sync_type is 'Remote' (document came from remote)"
			# )
			return {
				"status": "skipped",
				"doctype": doctype,
				"name": actual_name,
				"message": f"Document {doctype} {actual_name} has sync_type='Remote' and should not be synced to remote (it came from remote)"
			}
		
		# Also check sync_type from database if not in document object
		if frappe.db.has_column(doctype, 'sync_type'):
			db_sync_type = frappe.db.get_value(doctype, actual_name, 'sync_type')
			if db_sync_type == "Remote":
				return {
					"status": "skipped",
					"doctype": doctype,
					"name": actual_name,
					"message": f"Document {doctype} {actual_name} has sync_type='Remote' and should not be synced to remote (it came from remote)"
				}
			elif db_sync_type:
				frappe.logger().info(f"[SYNC] Document {doctype} {actual_name} has sync_type='{db_sync_type}' from database")
		
		
		# Check company filter if specified in settings
		if settings:
			company = getattr(settings, 'company', None)
			if company:
				# Check if document belongs to the specified company
				doc_data_check = doc.as_dict()
				if not belongs_to_company(doc_data_check, doctype, company):
					# Document doesn't belong to the selected company, skip syncing
					return {
						"status": "skipped",
						"doctype": doctype,
						"name": name,
						"message": f"Document belongs to different company (not {company}). Skipping sync."
					}
		
		# Handle naming series renaming for Payment Entry and Sales Invoice
		# Rename the document locally using the configured naming series, then sync with the new name
		naming_series_to_use = None
		if settings:
			if doctype == "Payment Entry" and hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series:
				naming_series_to_use = settings.payment_entry_naming_series
			elif doctype == "Sales Invoice" and hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series:
				naming_series_to_use = settings.sales_invoice_naming_series
		
		# For Sales Invoice and Payment Entry, set naming series in doc_data for remote to use
		# Do NOT rename the document locally - keep original name and let remote use the naming series
		
		# Sync linked documents first (dependencies) - for send direction
		# This ensures all referenced documents exist on the remote server
		# Skip if this is already a dependency sync (to avoid infinite recursion)
		link_field_mapping = {}
		if not force_create:
			try:
				link_field_mapping = sync_linked_documents(
					doc, api_client, settings, target_url, api_key, api_secret, direction="send"
				) or {}
			except Exception as e:
				# Log but don't fail - we'll try to sync anyway
				frappe.log_error(
					"Error syncing linked documents",
					f"Error syncing linked documents for {doctype} {name}: {str(e)}"
				)
		
		# Ensure sync fields exist on remote for syncable and compulsory doctypes
		try:
			ensure_sync_fields_exist_on_remote(doctype, api_client, settings)
		except Exception as e:
			# Log but don't fail - we'll try to sync anyway
			frappe.log_error(
				"Failed to ensure sync fields",
				f"Could not ensure sync fields exist on remote for {doctype}: {str(e)}"
			)
		
		# Prepare document data
		doc_data = prepare_doc_for_sync(doc)
		doc_data['doctype'] = doctype
		
		# Set the name field in doc_data
		# Always use actual_name (original name, not renamed)
		# For other doctypes, actual_name may have -Local suffix
		current_doc_name = actual_name
		
		doc_data['name'] = current_doc_name
		frappe.logger().info(f"Prepared doc_data for {doctype}, name field set to: {current_doc_name}")
		
		# For submittable doctypes, ensure docstatus is set correctly BEFORE setting sync fields
		# This ensures the document is synced with the correct status (submitted = 1, draft = 0)
		# IMPORTANT: Always use the actual docstatus from the document object, not from doc_data
		if is_submittable_doctype(doctype):
			# Always get docstatus directly from the document object
			docstatus_value = getattr(doc, 'docstatus', None)
			if docstatus_value is not None:
				doc_data['docstatus'] = docstatus_value
				frappe.logger().info(f"Set docstatus={docstatus_value} for submittable doctype {doctype} {current_doc_name} (from document object)")
			elif 'docstatus' in doc_data:
				# Use docstatus from doc_data if document doesn't have it
				frappe.logger().info(f"Using docstatus={doc_data['docstatus']} from doc_data for {doctype} {current_doc_name}")
			else:
				# Default to draft (0) if not specified
				doc_data['docstatus'] = 0
				frappe.log_error(
					f"docstatus not found for {doctype} {current_doc_name}",
					f"docstatus not found for {doctype} {current_doc_name}, defaulting to 0 (draft)"
				)
		else:
			# For non-submittable doctypes, ensure docstatus is 0 or not set
			if 'docstatus' in doc_data and doc_data.get('docstatus') == 1:
				frappe.log_error(
					f"Document {doctype} {current_doc_name} has docstatus=1 but is not submittable",
					f"Document {doctype} {current_doc_name} has docstatus=1 but is not submittable. Setting to 0."
				)
				doc_data['docstatus'] = 0
		
		# Update link field references to use remote names
		# This ensures references point to documents that exist on remote
		# First use link_field_mapping, then try to resolve using sync_reference for any remaining links
		for field in doc.meta.fields:
			if field.fieldtype == "Link" and field.options:
				link_doctype = field.options
				link_value = doc_data.get(field.fieldname)
				# Skip None, null, empty string, or the string "None"
				if not link_value or link_value in (None, "", "None", "null"):
					continue
					remote_name = None
					# Check if we have a mapping for this link
					mapping_key = (link_doctype, link_value)
					if mapping_key in link_field_mapping:
						remote_name = link_field_mapping[mapping_key]
						frappe.logger().info(f"Using link_field_mapping for {field.fieldname}: {link_value} -> {remote_name}")
					else:
						# Try to resolve using sync_reference
						remote_name = resolve_remote_document_name_by_sync_reference(
							api_client, link_doctype, link_value
						)
						if remote_name:
							frappe.logger().info(f"Resolved {field.fieldname} using sync_reference: {link_value} -> {remote_name}")
							# Store in mapping for future use
							link_field_mapping[mapping_key] = remote_name
					
					if remote_name and remote_name != link_value:
						doc_data[field.fieldname] = remote_name
						frappe.logger().info(f"Updated link field {field.fieldname} from {link_value} to {remote_name} for {doctype} {name}")
					elif not remote_name:
						# For critical links like Customer in Sales Invoice, ensure the document exists on remote
						# If it doesn't exist, try to sync it or create a minimal version
						if link_doctype == "Customer" and doctype in ("Sales Invoice", "Payment Entry", "Sales Order"):
							# Customer is critical for Sales Invoice - ensure it exists on remote
							try:
								# Check if customer exists on remote by name
								try:
									remote_customer = api_client.get_document("Customer", link_value)
									if remote_customer:
										# Customer exists with same name, use it
										doc_data[field.fieldname] = link_value
										frappe.logger().info(f"Customer {link_value} exists on remote with same name, using it for {doctype} {name}")
										continue
								except (DocumentNotFoundError, requests.exceptions.HTTPError):
									# Customer doesn't exist, try to sync it
									if frappe.db.exists("Customer", link_value):
										frappe.log_error(
										f"Customer {link_value} not found on remote for {doctype} {name}",
										f"Customer {link_value} not found on remote for {doctype} {name}. Attempting to sync customer first."
									)
										# Sync the customer first
										customer_sync_result = sync_document_to_remote(
											"Customer", link_value, target_url, api_key, api_secret, 
											force_create=True, sync_method="Auto", settings=settings
										)
										if customer_sync_result and customer_sync_result.get("status") == "success":
											# Try to resolve again after syncing
											remote_name = resolve_remote_document_name_by_sync_reference(
												api_client, "Customer", link_value
											) or link_value
											doc_data[field.fieldname] = remote_name
											frappe.logger().info(f"Synced customer {link_value} to remote, using {remote_name} for {doctype} {name}")
										else:
											frappe.log_error(
												"Failed to sync customer before Sales Invoice",
												f"Could not sync customer {link_value} to remote before syncing {doctype} {name}. Customer sync result: {customer_sync_result}"
											)
											# Use local name as fallback
											doc_data[field.fieldname] = link_value
									else:
										frappe.log_error(
											"Customer not found",
											f"Customer {link_value} referenced by {doctype} {name} does not exist locally or on remote."
										)
										# Use local name as fallback
										doc_data[field.fieldname] = link_value
							except Exception as customer_error:
								frappe.log_error(
									"Error ensuring customer exists on remote",
									f"Error ensuring customer {link_value} exists on remote for {doctype} {name}: {str(customer_error)}"
								)
								# Use local name as fallback
								doc_data[field.fieldname] = link_value
						else:
							frappe.log_error(
								f"Could not resolve remote name for {link_doctype} {link_value}",
								f"Could not resolve remote name for linked document {link_doctype} {link_value} in field {field.fieldname}. Using local name."
							)
							# Use local name as fallback
							doc_data[field.fieldname] = link_value
		
		# Also update link fields in child tables
		for field in doc.meta.fields:
			if field.fieldtype == "Table" and field.fieldname in doc_data:
				child_table_data = doc_data[field.fieldname]
				if isinstance(child_table_data, list):
					child_meta = frappe.get_meta(field.options)
					for child_row in child_table_data:
						if isinstance(child_row, dict):
							for child_field in child_meta.fields:
								if child_field.fieldtype == "Link" and child_field.options:
									link_doctype = child_field.options
									link_value = child_row.get(child_field.fieldname)
									# Skip None, null, empty string, or the string "None"
									if not link_value or link_value in (None, "", "None", "null"):
										continue
										remote_name = None
										# Check if we have a mapping for this link
										mapping_key = (link_doctype, link_value)
										if mapping_key in link_field_mapping:
											remote_name = link_field_mapping[mapping_key]
											frappe.logger().info(f"Using link_field_mapping for child {child_field.fieldname}: {link_value} -> {remote_name}")
										else:
											# Try to resolve using sync_reference
											remote_name = resolve_remote_document_name_by_sync_reference(
												api_client, link_doctype, link_value
											)
											if remote_name:
												frappe.logger().info(f"Resolved child {child_field.fieldname} using sync_reference: {link_value} -> {remote_name}")
												# Store in mapping for future use
												link_field_mapping[mapping_key] = remote_name
										
										if remote_name and remote_name != link_value:
											child_row[child_field.fieldname] = remote_name
											frappe.logger().info(f"Updated child table link field {child_field.fieldname} from {link_value} to {remote_name} in {field.fieldname}")
										elif not remote_name:
											frappe.log_error(
												f"Could not resolve remote name for {link_doctype} {link_value} in child field",
												f"Could not resolve remote name for linked document {link_doctype} {link_value} in child field {child_field.fieldname}. Using local name."
											)
		
		# For all syncable and compulsory doctypes, add sync_reference and sync_type
		# Check if doctype is syncable or compulsory
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		is_compulsory = doctype in auto_sync_doctypes
		is_syncable = should_sync_doctype(doctype, settings, direction="send") if settings else False
		
		# Ensure naming series exists on remote and in options, and set it in doc_data
		if naming_series_to_use:
			try:
				from havano_sync.havano_sync.tasks.remote_field_management import (
					ensure_naming_series_on_remote,
					ensure_naming_series_in_doctype_options
				)
				
				# Ensure naming series pattern is available on remote
				naming_series_exists = ensure_naming_series_on_remote(
					naming_series_to_use, api_client, target_url, api_key, api_secret, settings
				)
				if naming_series_exists:
					# Ensure it's in the doctype options
					ensure_naming_series_in_doctype_options(doctype, naming_series_to_use, api_client)
					frappe.logger().info(f"Ensured naming series {naming_series_to_use} is available on remote for {doctype} {actual_name}")
					
					# Set naming series in document data - remote will use this naming series
					doc_data['naming_series'] = naming_series_to_use
					frappe.logger().info(f"Set naming_series to {naming_series_to_use} in document data for {doctype} {actual_name}")
				else:
					frappe.log_error(
						f"Could not ensure naming series {naming_series_to_use} exists on remote",
						f"Could not ensure naming series {naming_series_to_use} exists on remote for {doctype} {actual_name}."
					)
			except Exception as e:
				frappe.log_error(
					"Failed to ensure naming series on remote",
					f"Could not ensure naming series {naming_series_to_use} exists on remote for {doctype} {actual_name}: {str(e)}"
				)
		else:
			# Use the current naming series from the document if no override is configured
			current_naming_series = doc.get('naming_series', '')
			if current_naming_series:
				doc_data['naming_series'] = current_naming_series
		
		if is_compulsory or is_syncable:
			# Set sync_reference to local document name (use current_doc_name which is the renamed name)
			# For Sales Invoice and Payment Entry with naming series, current_doc_name should be the renamed name
			# For other doctypes, current_doc_name may have -Local suffix
			# IMPORTANT: Use current_doc_name to ensure we use the renamed name after rename completes
			doc_data['sync_reference'] = current_doc_name
			# Set sync_type to "Local" (since this is being sent from local)
			doc_data['sync_type'] = "Local"
			# Note: docstatus is already set above for submittable doctypes, so we don't need to set it again here
		# else:
		# 	frappe.log_error(
		# 		f"[SYNC] {doctype} {actual_name} is NOT compulsory and NOT syncable",
		# 		f"[SYNC] {doctype} {actual_name} is NOT compulsory and NOT syncable - sync_reference and sync_type will NOT be set"
		# 	)
		
		# Clean up None values and empty strings that might cause issues
		# Convert None to empty string for string fields, remove None from dict
		# IMPORTANT: Preserve docstatus for submittable doctypes (0 or 1, not None)
		# IMPORTANT: Preserve critical link fields like customer, party, etc.
		def clean_data(data):
			"""Recursively clean data to remove None values that might cause issues"""
			# Critical fields that should never be removed even if None
			critical_fields = {'customer', 'party', 'supplier', 'company', 'naming_series', 'docstatus'}
			if isinstance(data, dict):
				cleaned = {}
				for k, v in data.items():
					if v is None:
						# Skip None values to avoid validation issues
						# BUT preserve docstatus even if it's 0 (draft) for submittable doctypes
						if k == 'docstatus' and is_submittable_doctype(doctype):
							cleaned[k] = 0  # Default to draft if None
							continue
						# Preserve critical fields even if None (they might be set later)
						if k in critical_fields:
							# For critical link fields, keep them if they exist in the original doc
							if k in ('customer', 'party', 'supplier') and hasattr(doc, k):
								original_value = getattr(doc, k, None)
								if original_value:
									cleaned[k] = original_value
									continue
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
		
		# After cleaning, ensure docstatus is still set correctly for submittable doctypes
		# This is critical - docstatus must be preserved for submitted documents (docstatus=1)
		# For non-submittable doctypes, ensure docstatus is 0
		# Also ensure name is explicitly set
		if is_submittable_doctype(doctype):
			if hasattr(doc, 'docstatus'):
				doc_data['docstatus'] = doc.docstatus
				frappe.logger().info(f"Re-ensured docstatus={doc.docstatus} for {doctype} {current_doc_name} after cleaning")
			elif 'docstatus' not in doc_data:
				# If docstatus not found, default to draft (0)
				doc_data['docstatus'] = 0
				frappe.log_error(
					f"docstatus not found for {doctype} {current_doc_name}",
					f"docstatus not found for {doctype} {current_doc_name}, defaulting to 0 (draft)"
				)
		else:
			# For non-submittable doctypes, ensure docstatus is 0
			if 'docstatus' in doc_data and doc_data.get('docstatus') == 1:
				frappe.log_error(
					f"Document {doctype} {current_doc_name} has docstatus=1 but is not submittable (after cleaning)",
					f"Document {doctype} {current_doc_name} has docstatus=1 but is not submittable. Setting to 0 after cleaning."
				)
				doc_data['docstatus'] = 0
		
		# Validate critical fields after cleaning
		# For Sales Invoice, Payment Entry, and Sales Order, ensure customer is set
		if doctype in ("Sales Invoice", "Payment Entry", "Sales Order"):
			customer_field = doc_data.get('customer') or doc_data.get('party')
			if not customer_field or customer_field in (None, "", "None", "null"):
				# Try to get from doc if not in doc_data
				customer_field = doc.get('customer') or doc.get('party')
				if customer_field:
					# Determine which field name to use
					if 'customer' in doc.as_dict():
						doc_data['customer'] = customer_field
					elif 'party' in doc.as_dict():
						doc_data['party'] = customer_field
					else:
						# Default to customer for Sales Invoice and Sales Order
						if doctype in ("Sales Invoice", "Sales Order"):
							doc_data['customer'] = customer_field
						else:
							doc_data['party'] = customer_field
					frappe.logger().info(f"Restored customer/party field {customer_field} for {doctype} {actual_name} after cleaning")
				else:
					frappe.log_error(
						f"Missing customer for {doctype}",
						f"{doctype} {actual_name} has no customer/party field set. Cannot sync without customer."
					)
					return {
						"status": "failed",
						"doctype": doctype,
						"name": name,
						"message": f"Cannot sync {doctype} without customer/party field"
					}
		
		# Check if document exists on remote BEFORE setting name in doc_data
		# This prevents duplicates and ensures we update the correct document
		doc_exists = False
		existing_doc_by_reference = None
		remote_document_name = None
		
		# For all syncable and compulsory doctypes, check if document exists by sync_reference
		# This prevents duplicates when syncing from local
		if is_compulsory or is_syncable:
			try:
				# Use custom API endpoint to find document by sync_reference
				# This works around the limitation that frappe.client.get_list doesn't allow custom fields in filters
				remote_name_by_ref = api_client.find_document_by_sync_reference(doctype, actual_name, sync_type="Local")
				if remote_name_by_ref:
					doc_exists = True
					remote_document_name = remote_name_by_ref
					existing_doc_by_reference = {"name": remote_name_by_ref}
					frappe.logger().info(f"Found existing document {doctype} {remote_name_by_ref} by sync_reference {actual_name}")
				else:
					# Fallback: try to get the document by name directly (in case names match)
					try:
						remote_doc = api_client.get_document(doctype, actual_name)
						# Check if sync_reference matches
						if remote_doc.get('sync_reference') == actual_name and remote_doc.get('sync_type') == 'Local':
							doc_exists = True
							remote_document_name = actual_name
							existing_doc_by_reference = {"name": actual_name}
							frappe.logger().info(f"Found existing document {doctype} {actual_name} by name (sync_reference matches)")
					except (DocumentNotFoundError, requests.exceptions.HTTPError):
						# Document doesn't exist with that name
						pass
			except Exception as ref_check_error:
				# If reference check fails, fall back to normal check
				frappe.logger().debug(f"Could not check by sync_reference: {str(ref_check_error)}")
		
		if not doc_exists and not force_create:
			try:
				# Try with actual_name first (which may have -Local suffix)
				remote_doc = api_client.get_document(doctype, actual_name)
				doc_exists = True
				remote_document_name = actual_name
				existing_doc_by_reference = {"name": actual_name}
			except (DocumentNotFoundError, requests.exceptions.HTTPError) as e:
				# Check if it's a 404 error (document not found)
				is_404 = False
				if isinstance(e, requests.exceptions.HTTPError) and e.response and e.response.status_code == 404:
					is_404 = True
				elif isinstance(e, DocumentNotFoundError):
					is_404 = True
				
				if is_404:
					# If not found with actual_name, try with original name
					try:
						if actual_name != name:
							remote_doc = api_client.get_document(doctype, name)
							doc_exists = True
							remote_document_name = name
							existing_doc_by_reference = {"name": name}
						else:
							# Document doesn't exist - this is expected, will create it
							doc_exists = False
					except (DocumentNotFoundError, requests.exceptions.HTTPError):
						# Document doesn't exist - this is expected, will create it
						doc_exists = False
				else:
					# Other errors (connection, auth, etc.) - try to create anyway
					doc_exists = False
			except Exception:
				# Other errors (connection, auth, etc.) - try to create anyway
				doc_exists = False
		
		# Set name in doc_data based on whether we're updating or creating
		if doc_exists and not force_create:
			# For updates, use the remote document's name (not the local name with -Local suffix)
			# This prevents renaming and ensures we update the correct document
			remote_document_name = existing_doc_by_reference.get('name') if existing_doc_by_reference else name
			doc_data['name'] = remote_document_name
			frappe.logger().info(f"Updating existing document {doctype} {remote_document_name} (local name: {actual_name})")
		else:
			# For new documents, check if document exists by name OR by sync_reference
			# This prevents creating duplicates
			doc_data['name'] = current_doc_name
			try:
				# First check by name
				api_client.get_document(doctype, current_doc_name)
				doc_exists = True
				remote_document_name = current_doc_name
				frappe.logger().info(f"Document {doctype} {current_doc_name} exists by name, will update")
			except (DocumentNotFoundError, requests.exceptions.HTTPError):
				# Not found by name, check by sync_reference
				if is_compulsory or is_syncable:
					# Check by actual_name (local document name)
					remote_name_by_ref = api_client.find_document_by_sync_reference(doctype, actual_name, sync_type="Local")
					if remote_name_by_ref:
						doc_exists = True
						remote_document_name = remote_name_by_ref
						doc_data['name'] = remote_name_by_ref
						frappe.logger().info(f"Document {doctype} found by sync_reference {actual_name} as {remote_name_by_ref}, will update")
					else:
						# Also check by the sync_reference value we're about to set (in case it's different)
						sync_ref_value = doc_data.get('sync_reference')
						if sync_ref_value and sync_ref_value != actual_name:
							remote_name_by_ref2 = api_client.find_document_by_sync_reference(doctype, sync_ref_value, sync_type="Local")
							if remote_name_by_ref2:
								doc_exists = True
								remote_document_name = remote_name_by_ref2
								doc_data['name'] = remote_name_by_ref2
								frappe.logger().info(f"Document {doctype} found by sync_reference {sync_ref_value} as {remote_name_by_ref2}, will update")
							else:
								doc_exists = False
								frappe.logger().info(f"Creating new document {doctype} with name {current_doc_name}")
						else:
							doc_exists = False
							frappe.logger().info(f"Creating new document {doctype} with name {current_doc_name}")
				else:
					doc_exists = False
					frappe.logger().info(f"Creating new document {doctype} with name {current_doc_name}")
		
		# Create or update document
		action = None  # Initialize action variable
		if doc_exists and not force_create:
			update_name = doc_data.get('name') or remote_document_name or name
			try:
				# Verify document exists before updating
				try:
					api_client.get_document(doctype, update_name)
				except (DocumentNotFoundError, requests.exceptions.HTTPError) as e:
					if isinstance(e, requests.exceptions.HTTPError) and e.response and e.response.status_code == 404:
						# Document doesn't exist, try to create instead
						# frappe.log_error(
						# 	f"[SYNC] Document {doctype} {update_name} not found",
						# 	f"Document {doctype} {update_name} not found, creating instead"
						# )
						doc_exists = False
					else:
						raise
				
				if doc_exists:
					result = api_client.update_document(doctype, update_name, doc_data)
					action = "updated"
			except requests.exceptions.HTTPError as update_error:
				error_str = str(update_error)
				if "LinkValidationError" in error_str or "Could not find" in error_str:
					handled = handle_link_validation_error(
						update_error, doctype, name, api_client, settings,
						target_url, api_key, api_secret, direction="send"
					)
					if handled:
						try:
							result = api_client.update_document(doctype, update_name, doc_data)
							action = "updated"
						except Exception:
							raise update_error
					else:
						raise update_error
				else:
					raise
		else:
			# For new documents, name is already set above to current_doc_name
			# For submittable doctypes, try to create directly with docstatus=1
			# If that fails, fall back to creating as draft then submitting
			needs_submit = False
			original_docstatus = None
			try_create_with_docstatus_1 = False
			
			# Double-check that doctype is actually submittable before trying to submit
			if is_submittable_doctype(doctype):
				# Preserve the original docstatus for submittable doctypes
				if hasattr(doc, 'docstatus'):
					original_docstatus = doc.docstatus
					doc_data['docstatus'] = doc.docstatus
					frappe.logger().info(f"Submittable doctype {doctype} {current_doc_name} has docstatus={original_docstatus}")
				elif 'docstatus' in doc_data:
					original_docstatus = doc_data.get('docstatus')
					frappe.logger().info(f"Submittable doctype {doctype} {current_doc_name} has docstatus={original_docstatus} from doc_data")
			else:
					# Default to draft if not specified
					original_docstatus = 0
					doc_data['docstatus'] = 0
					frappe.log_error(
						f"Submittable doctype {doctype} {current_doc_name} has no docstatus",
						f"Submittable doctype {doctype} {current_doc_name} has no docstatus, defaulting to 0 (draft)"
					)
				
			# If docstatus is 1, try to create directly with docstatus=1 first
			if is_submittable_doctype(doctype):
				if original_docstatus == 1:
					try_create_with_docstatus_1 = True
					# Keep docstatus=1 in doc_data to attempt direct creation
					doc_data['docstatus'] = 1
					frappe.logger().info(f"Attempting to create submittable document {doctype} {current_doc_name} directly with docstatus=1")
				else:
					# Keep docstatus as is (0 for draft)
					frappe.logger().info(f"Creating submittable document {doctype} {current_doc_name} as draft (docstatus={original_docstatus})")
			else:
				# For non-submittable doctypes, ensure docstatus is 0
				if 'docstatus' in doc_data and doc_data.get('docstatus') == 1:
					# If docstatus is 1 but doctype is not submittable, set to 0
					frappe.log_error(
						f"Document {doctype} {current_doc_name} has docstatus=1 but is not submittable",
						f"Document {doctype} {current_doc_name} has docstatus=1 but is not submittable. Setting to 0."
					)
					doc_data['docstatus'] = 0
				elif 'docstatus' not in doc_data:
					doc_data['docstatus'] = 0
				frappe.logger().info(f"Creating document {doctype} with name {current_doc_name} on remote (actual_name was: {actual_name}, doc.name is: {doc.name if hasattr(doc, 'name') else 'N/A'})")
			
			try:
				# For submittable doctypes with docstatus=1, try creating directly with docstatus=1
				# If that fails, fall back to creating as draft then submitting
				if try_create_with_docstatus_1:
					try:
						frappe.logger().info(f"Attempting to create {doctype} {current_doc_name} directly with docstatus=1")
						result = api_client.create_document(doctype, doc_data)
						action = "created"
						frappe.logger().info(f"Successfully created {doctype} {current_doc_name} directly with docstatus=1")
						# If successful, we don't need to submit
						needs_submit = False
					except Exception as create_error:
						error_str = str(create_error)
						# If creation with docstatus=1 fails, fall back to creating as draft
						frappe.log_error(
							f"Failed to create {doctype} {current_doc_name} with docstatus=1",
							f"Failed to create {doctype} {current_doc_name} with docstatus=1: {error_str}. Falling back to create as draft then submit."
						)
						# Set docstatus to 0 for draft creation
						doc_data['docstatus'] = 0
						needs_submit = True
						# Retry creation as draft
						# Before retrying, ensure link fields are resolved
						# Update link field references to use remote names (in case they weren't resolved earlier)
						for field in doc.meta.fields:
							if field.fieldtype == "Link" and field.options:
								link_doctype = field.options
								link_value = doc_data.get(field.fieldname)
								if link_value:
									# Try to resolve using sync_reference if not already in mapping
									mapping_key = (link_doctype, link_value)
									if mapping_key not in link_field_mapping:
										remote_name = resolve_remote_document_name_by_sync_reference(
											api_client, link_doctype, link_value
										)
										if remote_name:
											doc_data[field.fieldname] = remote_name
											link_field_mapping[mapping_key] = remote_name
											frappe.logger().info(f"Resolved {field.fieldname} using sync_reference before retry: {link_value} -> {remote_name}")
						
						# Also check child tables
						for field in doc.meta.fields:
							if field.fieldtype == "Table" and field.fieldname in doc_data:
								child_table_data = doc_data[field.fieldname]
								if isinstance(child_table_data, list):
									child_meta = frappe.get_meta(field.options)
									for child_row in child_table_data:
										if isinstance(child_row, dict):
											for child_field in child_meta.fields:
												if child_field.fieldtype == "Link" and child_field.options:
													link_doctype = child_field.options
													link_value = child_row.get(child_field.fieldname)
													if link_value:
														mapping_key = (link_doctype, link_value)
														if mapping_key not in link_field_mapping:
															remote_name = resolve_remote_document_name_by_sync_reference(
																api_client, link_doctype, link_value
															)
															if remote_name:
																child_row[child_field.fieldname] = remote_name
																link_field_mapping[mapping_key] = remote_name
																frappe.logger().info(f"Resolved child {child_field.fieldname} using sync_reference before retry: {link_value} -> {remote_name}")
						
						result = api_client.create_document(doctype, doc_data)
						action = "created"
						frappe.logger().info(f"Created {doctype} {current_doc_name} as draft (docstatus=0), will submit after creation")
				else:
					# Normal creation (draft or non-submittable)
					result = api_client.create_document(doctype, doc_data)
					action = "created"
				
				# After creating, check if remote used the name we sent or generated a new one
				# If remote generated a new name, we need to rename it to match our local name (with -Local suffix)
				# Frappe API returns the document dict directly, or wrapped in 'message'
				created_name = None
				if result:
					if isinstance(result, dict):
						# Try different possible locations for the name
						created_name = result.get('name') or result.get('data', {}).get('name')
						if not created_name and 'message' in result:
							# Sometimes the result is wrapped
							msg = result.get('message')
							if isinstance(msg, dict):
								created_name = msg.get('name')
					elif isinstance(result, str):
						# Sometimes Frappe returns just the name as a string
						created_name = result
				
				frappe.logger().info(f"Created name from remote: {created_name}, expected: {current_doc_name}, actual_name was: {actual_name}")
				
				# Set sync_reference and sync_type immediately after creation
				# This ensures sync_reference is available for linked document resolution
				# For submittable doctypes created with docstatus=1, sync_reference is set immediately
				# For submittable doctypes created as draft, sync_reference is set before submitting
				if (is_compulsory or is_syncable) and created_name:
					try:
						# Use actual_name (renamed name from local) for sync_reference
						# This allows us to find the remote document by searching for sync_reference
						update_data = {
							'sync_reference': current_doc_name,  # Use current_doc_name which is the renamed name from local
							'sync_type': 'Local'
						}
						api_client.update_document(doctype, created_name, update_data)
						frappe.logger().info(f"Set sync_reference={current_doc_name} and sync_type=Local on remote {doctype} {created_name}")
					except Exception as update_error:
						frappe.log_error(
							"Failed to set sync_reference on remote document",
							f"Could not set sync_reference on remote {doctype} {created_name}: {str(update_error)}"
						)
				
				# For submittable doctypes that were created as draft, queue submit in background after a short delay
				# This avoids errors from trying to submit too quickly after creation
				# If document was created with docstatus=1 directly, needs_submit will be False and we skip this
				if needs_submit and created_name and is_submittable_doctype(doctype):
					# Queue submit in background - wait 5 seconds after creation
					frappe.enqueue(
						_submit_remote_document,
						doctype=doctype,
						document_name=created_name,
						remote_url=settings.remote_url,
						admin_api_key=settings.admin_api_key,
						admin_api_secret=get_decrypted_api_secret(settings),
						queue="short",
						timeout=300,
						is_async=True,
						job_name=f"submit_{doctype}_{created_name}"
					)
					frappe.logger().info(f"Queued submit for {doctype} {created_name} to run after 5 seconds")
				elif needs_submit and created_name and not is_submittable_doctype(doctype):
					# This shouldn't happen, but log a warning if it does
					frappe.log_error(
						f"Attempted to submit non-submittable doctype {doctype} {created_name}",
						f"Attempted to submit non-submittable doctype {doctype} {created_name}. Skipping submit."
					)
			except DuplicateEntryError:
				# Document already exists - find it and update instead
				# The duplicate error means a document with the name in doc_data already exists
				attempted_name = doc_data.get('name') or current_doc_name
				frappe.logger().info(f"Document {doctype} {attempted_name} already exists on remote. Finding and updating.")
				
				update_name = None
				# First try the name we attempted to create (most likely to exist)
				try:
					api_client.get_document(doctype, attempted_name)
					update_name = attempted_name
				except (DocumentNotFoundError, requests.exceptions.HTTPError):
					# Not found by attempted name, try current_doc_name
					try:
						api_client.get_document(doctype, current_doc_name)
						update_name = current_doc_name
					except (DocumentNotFoundError, requests.exceptions.HTTPError):
						# Not found by name, try by sync_reference
						if is_compulsory or is_syncable:
							update_name = api_client.find_document_by_sync_reference(doctype, actual_name, sync_type="Local")
				
				# If still not found, use attempted_name as fallback
				if not update_name:
					update_name = attempted_name
				
				# Verify and update
				try:
					# Verify it exists
					api_client.get_document(doctype, update_name)
					result = api_client.update_document(doctype, update_name, doc_data)
					action = "updated"
					frappe.logger().info(f"Updated existing document {doctype} {update_name}")
				except (DocumentNotFoundError, requests.exceptions.HTTPError) as e:
					# Document doesn't exist - this shouldn't happen after duplicate error
					# But handle it gracefully by trying to create again without the name
					if isinstance(e, requests.exceptions.HTTPError) and e.response and e.response.status_code == 404:
						frappe.log_error(
							f"Document {doctype} {update_name} not found after DuplicateEntryError",
							f"Document {doctype} {update_name} not found after DuplicateEntryError. Removing name from doc_data and retrying create."
						)
						# Remove name and let remote generate a new one
						doc_data.pop('name', None)
						try:
							result = api_client.create_document(doctype, doc_data)
							action = "created"
							frappe.logger().info(f"Created document {doctype} without name after duplicate error")
						except (DuplicateEntryError, Exception) as retry_error:
							# If duplicate entry again or any other error, just pass - document already exists
							if isinstance(retry_error, DuplicateEntryError):
								frappe.logger().info(f"Document {doctype} already exists on remote (duplicate entry). Skipping sync.")
								action = "skipped"
								result = {"name": update_name or attempted_name}
							else:
								frappe.log_error(
									"Failed to create document after duplicate error",
									f"Could not create {doctype} after DuplicateEntryError: {str(retry_error)}"
								)
								# Pass instead of raising - document likely already exists
								action = "skipped"
								result = {"name": update_name or attempted_name}
					else:
						# For other errors, just pass - document likely already exists
						frappe.logger().info(f"Document {doctype} {update_name} already exists on remote. Skipping sync.")
						action = "skipped"
						result = {"name": update_name or attempted_name}
				except Exception as update_error:
					# Any other error during update - just pass, document likely already exists
					frappe.logger().info(f"Error updating document {doctype} {update_name} after duplicate entry: {str(update_error)}. Skipping sync.")
					action = "skipped"
					result = {"name": update_name or attempted_name}
			except requests.exceptions.HTTPError as create_error:
				# Check if it's a 403 Permission Denied error
				error_str = str(create_error)
				if create_error.response and create_error.response.status_code == 403:
					# Permission denied - log with detailed instructions and return error
					detailed_error = (
						f"403 Forbidden: Permission Denied\n\n"
						f"The API user associated with the Admin API Key does not have permission to create/update '{doctype}' documents on the remote server.\n\n"
						f"To fix this:\n"
						f"1. Go to the remote Frappe instance\n"
						f"2. Find the user associated with the Admin API Key\n"
						f"3. Assign 'System Manager' role OR grant appropriate permissions for '{doctype}'\n"
						f"4. Check doctype permissions in Settings > Permissions\n"
						f"5. Verify API credentials in Havano Sync Settings\n\n"
						f"Error details: {error_str}"
					)
					frappe.log_error(
						f"Sync Failed: Permission Denied for {doctype} {name}",
						detailed_error
					)
					return {
						"status": "error",
						"error": f"Permission Denied (403): The API user does not have permission to create/update '{doctype}' documents on the remote server. Please check the API user's permissions and roles on the remote instance.",
						"message": f"Failed to sync {doctype} {name}: Permission denied (403)"
					}
				# Check if it's a UniqueValidationError for sync_reference (417 status code)
				# This happens when a document with the same sync_reference already exists
				elif create_error.response and create_error.response.status_code == 417 and ("UniqueValidationError" in error_str or "sync_reference" in error_str.lower() or "Duplicate entry" in error_str):
					# Document with same sync_reference already exists - find it and update instead
					attempted_sync_ref = doc_data.get('sync_reference')
					if attempted_sync_ref and (is_compulsory or is_syncable):
						frappe.logger().info(f"Document with sync_reference {attempted_sync_ref} already exists on remote. Finding and updating.")
						# Try to find by sync_reference
						update_name = api_client.find_document_by_sync_reference(doctype, attempted_sync_ref, sync_type="Local")
						if update_name:
							try:
								result = api_client.update_document(doctype, update_name, doc_data)
								action = "updated"
								frappe.logger().info(f"Updated existing document {doctype} {update_name} with same sync_reference")
							except Exception as update_error:
								frappe.log_error(
									"Failed to update document with duplicate sync_reference",
									f"Could not update {doctype} {update_name} after UniqueValidationError: {str(update_error)}"
								)
								# Pass instead of raising - document likely already exists
								action = "skipped"
								result = {"name": update_name}
						else:
							# Couldn't find by sync_reference, just pass
							frappe.logger().info(f"Could not find document with sync_reference {attempted_sync_ref} on remote. Skipping sync.")
							action = "skipped"
							result = {"name": current_doc_name}
					else:
						# No sync_reference or not syncable, just pass
						frappe.logger().info(f"UniqueValidationError for {doctype} {current_doc_name} (sync_reference issue). Skipping sync.")
						action = "skipped"
						result = {"name": current_doc_name}
				# Check if it's a LinkValidationError (417 status code or error message)
				elif (create_error.response and create_error.response.status_code == 417) or "LinkValidationError" in error_str or "Could not find" in error_str:
					# Try to handle missing linked documents
					frappe.logger().info(f"LinkValidationError detected for {doctype} {name} (status: {create_error.response.status_code if create_error.response else 'unknown'}), attempting to resolve missing documents")
					
					# First, try to resolve any remaining link references using sync_reference
					# This handles cases where linked documents exist on remote but with different names
					resolved_any = False
					for field in doc.meta.fields:
						if field.fieldtype == "Link" and field.options:
							link_doctype = field.options
							link_value = doc_data.get(field.fieldname)
							# Skip None, null, empty string, or the string "None"
							if not link_value or link_value in (None, "", "None", "null"):
								continue
								# Try to resolve using sync_reference
								remote_name = resolve_remote_document_name_by_sync_reference(
									api_client, link_doctype, link_value
								)
								if remote_name and remote_name != link_value:
									doc_data[field.fieldname] = remote_name
									resolved_any = True
									frappe.logger().info(f"Resolved link field {field.fieldname} using sync_reference: {link_value} -> {remote_name}")
								elif not remote_name:
									# If sync_reference resolution failed, try to sync the missing document
									frappe.log_error(
										f"Could not resolve {link_doctype} {link_value} using sync_reference",
										f"Could not resolve {link_doctype} {link_value} using sync_reference. Attempting to sync it first."
									)
									try:
										# Check if document exists locally
										if frappe.db.exists(link_doctype, link_value):
											# Sync the missing document
											from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
											sync_result = sync_document_to_remote(
												link_doctype, link_value, settings, target_url, api_key, api_secret, force_create=False
											)
											if sync_result and sync_result.get("status") == "success":
												# Try to resolve again after syncing
												remote_name = resolve_remote_document_name_by_sync_reference(
													api_client, link_doctype, link_value
												)
												if remote_name:
													doc_data[field.fieldname] = remote_name
													resolved_any = True
													frappe.logger().info(f"Resolved link field {field.fieldname} after syncing: {link_value} -> {remote_name}")
										else:
											frappe.log_error(
												f"Linked document {link_doctype} {link_value} does not exist locally",
												f"Linked document {link_doctype} {link_value} does not exist locally. Cannot sync."
											)
									except Exception as sync_error:
										frappe.log_error(
											f"Failed to sync missing linked document {link_doctype} {link_value}",
											f"Failed to sync missing linked document {link_doctype} {link_value}: {str(sync_error)}"
										)
					
					# Also check child tables
					for field in doc.meta.fields:
						if field.fieldtype == "Table" and field.fieldname in doc_data:
							child_table_data = doc_data[field.fieldname]
							if isinstance(child_table_data, list):
								child_meta = frappe.get_meta(field.options)
								for idx, child_row in enumerate(child_table_data):
									if isinstance(child_row, dict):
										for child_field in child_meta.fields:
											if child_field.fieldtype == "Link" and child_field.options:
												link_doctype = child_field.options
												link_value = child_row.get(child_field.fieldname)
												# Skip None, null, empty string, or the string "None"
												if not link_value or link_value in (None, "", "None", "null"):
													continue
													# Try to resolve using sync_reference
													remote_name = resolve_remote_document_name_by_sync_reference(
														api_client, link_doctype, link_value
													)
													if remote_name and remote_name != link_value:
														child_row[child_field.fieldname] = remote_name
														resolved_any = True
														frappe.logger().info(f"Resolved child link field {child_field.fieldname} (Row #{idx+1}) using sync_reference: {link_value} -> {remote_name}")
													elif not remote_name:
														# If sync_reference resolution failed, try to sync the missing document
														frappe.log_error(
															f"Could not resolve {link_doctype} {link_value} using sync_reference (child field)",
															f"Could not resolve {link_doctype} {link_value} using sync_reference. Attempting to sync it first."
														)
														try:
															# Check if document exists locally
															if frappe.db.exists(link_doctype, link_value):
																# Sync the missing document
																from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
																sync_result = sync_document_to_remote(
																	link_doctype, link_value, settings, target_url, api_key, api_secret, force_create=False
																)
																if sync_result and sync_result.get("status") == "success":
																	# Try to resolve again after syncing
																	remote_name = resolve_remote_document_name_by_sync_reference(
																		api_client, link_doctype, link_value
																	)
																	if remote_name:
																		child_row[child_field.fieldname] = remote_name
																		resolved_any = True
																		frappe.logger().info(f"Resolved child link field {child_field.fieldname} (Row #{idx+1}) after syncing: {link_value} -> {remote_name}")
															else:
																frappe.log_error(
																	f"Linked document {link_doctype} {link_value} does not exist locally (child field)",
																	f"Linked document {link_doctype} {link_value} does not exist locally. Cannot sync."
																)
														except Exception as sync_error:
															frappe.log_error(
																f"Failed to sync missing linked document {link_doctype} {link_value} (child field)",
																f"Failed to sync missing linked document {link_doctype} {link_value}: {str(sync_error)}"
															)
					
					# If we resolved any links, retry immediately
					if resolved_any:
						try:
							frappe.logger().info(f"Retrying create for {doctype} {name} after resolving link references using sync_reference")
							result = api_client.create_document(doctype, doc_data)
							action = "created"
							# Get created_name from result
							created_name = None
							if result:
								if isinstance(result, dict):
									created_name = result.get('name') or result.get('data', {}).get('name')
									if not created_name and 'message' in result:
										msg = result.get('message')
										if isinstance(msg, dict):
											created_name = msg.get('name')
							elif isinstance(result, str):
								created_name = result
							
							if created_name:
								frappe.logger().info(f"Created name from remote after link resolution: {created_name}")
								# Continue with normal flow - set sync_reference, queue submit if needed, etc.
								# We'll break out of the exception handler and continue with normal flow
								# by setting a flag and re-raising a special exception that we'll catch
								# Actually, we can just continue - the code after the try block will handle it
								# But we need to make sure we don't fall through to the error handling
								# So we'll set result and created_name, then break out
								# Actually, the best way is to just let it fall through - but we need to make sure
								# the exception handler doesn't catch it. Let's use a flag.
								# Actually, since we're in an exception handler, we need to re-raise or return
								# Let's just set the variables and then continue with the normal flow below
								# We'll need to extract the post-creation logic into a function or duplicate it
								# For now, let's just continue - we'll handle sync_reference setting below
								pass
							# If successful, we'll continue to the sync_reference setting code below
							# But we're still in the exception handler, so we need to break out
							# Let's use a different approach - set a flag and re-raise a special exception
							# Actually, the simplest is to just let it fall through and handle it in the outer try
							# But we're already in an exception handler...
							# Let's just continue - we'll set sync_reference in the code after the try block
							# But wait, that code won't execute if we're in the exception handler
							# So we need to manually execute it here or refactor
							# For now, let's just set the variables and let the code continue
							# We'll need to manually call the sync_reference setting code
							# Actually, let's just break out by not raising an exception
							# We'll set a flag to indicate success and continue
							# Actually, the simplest is to just not catch this exception and let it fall through
							# But we're already catching it...
							# Let's use a different approach - we'll set result and created_name, then
							# manually execute the post-creation code
							# Actually, I think the best approach is to extract the post-creation logic
							# But for now, let's just duplicate the critical parts here
							# Set sync_reference immediately after creation
							if (is_compulsory or is_syncable) and created_name:
								try:
									update_data = {
										'sync_reference': actual_name,  # Use actual_name which is the renamed name from local
										'sync_type': 'Local'
									}
									api_client.update_document(doctype, created_name, update_data)
									frappe.logger().info(f"Set sync_reference={actual_name} and sync_type=Local on remote {doctype} {created_name}")
								except Exception as update_error:
									frappe.log_error(
										"Failed to set sync_reference on remote document",
										f"Could not set sync_reference on remote {doctype} {created_name}: {str(update_error)}"
									)
							# Queue submit if needed
							if needs_submit and created_name and is_submittable_doctype(doctype):
								frappe.enqueue(
									_submit_remote_document,
									doctype=doctype,
									document_name=created_name,
									remote_url=settings.remote_url,
									admin_api_key=settings.admin_api_key,
									admin_api_secret=get_decrypted_api_secret(settings),
									queue="short",
									timeout=300,
									is_async=True,
									job_name=f"submit_{doctype}_{created_name}"
								)
							# Return success - we've handled everything
							return {
								"status": "success",
								"action": action,
								"doctype": doctype,
								"name": name,
								"remote_name": created_name,
								"message": f"Successfully synced {doctype} {name} to remote as {created_name}"
							}
						except Exception as retry_error:
							# If retry still fails, try handle_link_validation_error
							frappe.log_error(
								"Retry after sync_reference resolution failed",
								f"Retry after sync_reference resolution failed: {str(retry_error)}. Trying handle_link_validation_error."
							)
							handled = handle_link_validation_error(
								create_error, doctype, name, api_client, settings,
								target_url, api_key, api_secret, direction="send"
							)
							if handled:
								# Retry the create operation
								try:
									frappe.logger().info(f"Retrying create for {doctype} {name} after handling LinkValidationError")
									result = api_client.create_document(doctype, doc_data)
									action = "created"
									# Get created_name and continue
									created_name = None
									if result:
										if isinstance(result, dict):
											created_name = result.get('name') or result.get('data', {}).get('name')
											if not created_name and 'message' in result:
												msg = result.get('message')
												if isinstance(msg, dict):
													created_name = msg.get('name')
										elif isinstance(result, str):
											created_name = result
									if created_name:
										frappe.logger().info(f"Created name from remote after LinkValidationError handling: {created_name}")
								except Exception as retry_error2:
									# If retry still fails, raise the original error
									raise create_error
							else:
								# Could not handle the error, raise it
								raise create_error
					else:
						# No links resolved, try handle_link_validation_error
						handled = handle_link_validation_error(
							create_error, doctype, name, api_client, settings,
							target_url, api_key, api_secret, direction="send"
						)
						if handled:
							# Retry the create operation
							try:
								frappe.logger().info(f"Retrying create for {doctype} {name} after handling LinkValidationError")
								result = api_client.create_document(doctype, doc_data)
								action = "created"
							except Exception as retry_error:
								# If retry still fails, raise the original error
								raise create_error
						else:
							# Could not handle the error, raise it
							raise create_error
				else:
					# Re-raise other errors
					raise
			except Exception as create_error:
				# Check if it's a duplicate entry error in the error message
				if "DuplicateEntryError" in str(type(create_error)) or "Duplicate entry" in str(create_error) or "already exists" in str(create_error).lower():
					# Document already exists - just pass, don't throw error
					frappe.logger().info(f"Document {doctype} {name} already exists on remote. Skipping sync.")
					action = "skipped"
					result = {"name": name}
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
				# Import here to avoid circular dependency
				from havano_sync.havano_sync.tasks.fetch_operations import fetch_all_documents_from_remote
				frappe.enqueue(
					"havano_sync.havano_sync.tasks.fetch_operations.fetch_all_documents_from_remote",
					doctype=doctype,
					queue="default",
					timeout=300,
					is_async=True
				)
			except Exception as fetch_error:
				frappe.log_error(
					f"Could not trigger fetch for {doctype} after send",
					f"Could not trigger fetch for {doctype} after send: {str(fetch_error)}"
				)
		
		# Ensure action is set
		if 'action' not in locals():
			action = "unknown"
			frappe.log_error(
				f"[SYNC] Action was not set for {doctype} {actual_name}",
				f"[SYNC] Action was not set for {doctype} {actual_name}, defaulting to 'unknown'"
			)
		
		
		# After successful sync, rename remote document if sync_reference is set and different from remote name
		# Only for Sales Invoice, Payment Entry, and Quotation
		if action in ("created", "updated") and doctype in ("Sales Invoice", "Payment Entry", "Quotation") and settings and hasattr(doc, 'sync_reference') and doc.sync_reference:
			try:
				# Get the remote document name
				remote_doc_name = None
				if action == "created":
					# For created documents, get the name from result or created_name variable
					if 'created_name' in locals() and created_name:
						remote_doc_name = created_name
					elif result:
						if isinstance(result, dict):
							remote_doc_name = result.get('name') or result.get('data', {}).get('name')
							if not remote_doc_name and 'message' in result:
								msg = result.get('message')
								if isinstance(msg, dict):
									remote_doc_name = msg.get('name')
						elif isinstance(result, str):
							remote_doc_name = result
					# Fallback to current_doc_name if available
					if not remote_doc_name and 'current_doc_name' in locals():
						remote_doc_name = current_doc_name
				elif action == "updated":
					# For updated documents, use the update_name or doc_data name
					if 'update_name' in locals() and update_name:
						remote_doc_name = update_name
					else:
						remote_doc_name = doc_data.get('name') or actual_name
				
				# Check if remote name is different from sync_reference
				if remote_doc_name and remote_doc_name != doc.sync_reference:
					# Queue rename in background
					from havano_sync.havano_sync.tasks.sync_operations import _rename_single_remote_document
					frappe.enqueue(
						_rename_single_remote_document,
						doctype=doctype,
						remote_name=remote_doc_name,
						sync_reference=doc.sync_reference,
						settings=settings,
						queue="short",
						timeout=60,
						is_async=True,
						job_name=f"rename_remote_{doctype}_{remote_doc_name}"
					)
			except Exception as rename_queue_error:
				frappe.log_error(
					f"[SYNC] Failed to queue remote rename for {doctype}",
					f"[SYNC] Could not queue remote rename for {doctype}: {str(rename_queue_error)}"
				)
		
		# Update sync_status to "Synced" after successful sync (for all syncable doctypes)
		try:
			from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists, should_sync_doctype
			if settings and should_sync_doctype(doctype, settings, direction="send"):
				ensure_sync_status_field_exists(doctype)
				if frappe.db.has_column(doctype, 'sync_status'):
					frappe.db.set_value(doctype, actual_name, 'sync_status', 'Synced', update_modified=False)
					frappe.db.commit()
					frappe.logger().info(f"Set sync_status='Synced' for {doctype} {actual_name}")
		except Exception as sync_status_error:
			frappe.log_error(
				f"Failed to update sync_status for {doctype} {actual_name}",
				f"Error updating sync_status: {str(sync_status_error)}"
			)
		
		return {
			"status": "success",
			"doctype": doctype,
			"name": actual_name,  # Return actual_name (renamed name) instead of original name
			"action": action
		}
	except Exception as e:
		# Catch any other unexpected errors
		import traceback
		traceback_str = traceback.format_exc()
		error_msg = str(e)
		detailed_error = f"Unexpected error during sync: {error_msg}\n\nTraceback:\n{traceback_str}"
		
		duration = time.time() - start_time
		
		create_sync_log(
			sync_type="Send",
			doctype=doctype,
			document_name=name,
			status="Failed",
			message=f"Sync failed: {detailed_error}",
			error_details={"error": error_msg, "detailed_error": detailed_error, "traceback": traceback_str},
			sync_method=sync_method,
			duration_seconds=duration
		)
		
		frappe.log_error(
			f"Sync Failed: {doctype} {name}",
			traceback_str
		)
		
		return {
			"status": "error",
			"doctype": doctype,
			"name": name,
			"error": detailed_error
		}


def _submit_remote_document(doctype: str, document_name: str, remote_url: str, admin_api_key: str, admin_api_secret: str):
	"""
	Submit remote document after delay with retry logic
	This runs in background to avoid errors from trying to submit too quickly after creation
	
	Args:
		doctype: Document type
		document_name: Name of document on remote to submit
		remote_url: Remote server URL
		admin_api_key: Admin API key
		admin_api_secret: Admin API secret
	"""
	try:
		# Wait 5 seconds before submitting (allows document to be fully ready)
		frappe.logger().info(f"[SUBMIT] Waiting 5 seconds before submitting {doctype} {document_name}")
		time.sleep(5)
		frappe.logger().info(f"[SUBMIT] 5 seconds elapsed, proceeding with submit for {doctype} {document_name}")
		
		# Create API client
		api_client = SyncAPI(remote_url, admin_api_key, admin_api_secret)
		
		# Verify document exists and is in draft state before proceeding
		try:
			doc_check = api_client.get_document(doctype, document_name)
			current_docstatus = doc_check.get('docstatus', 0)
			frappe.logger().info(f"[SUBMIT] Document {doctype} {document_name} exists with docstatus={current_docstatus}")
			
			if current_docstatus != 0:
				frappe.logger().info(f"[SUBMIT] Document {doctype} {document_name} is already submitted/cancelled (docstatus={current_docstatus}). No need to submit.")
				return
		except Exception as check_error:
			frappe.log_error(
				"Failed to verify document before submit",
				f"Could not verify document {doctype} {document_name} exists before submit: {str(check_error)}"
			)
			return
		
		# Now submit the document to get docstatus=1 with retry logic
		max_retries = 3
		retry_delay = 5  # seconds
		submit_success = False
		
		for attempt in range(1, max_retries + 1):
			try:
				frappe.logger().info(f"[SUBMIT] Attempt {attempt}/{max_retries}: Submitting document {doctype} {document_name} on remote to set docstatus=1")
				
				# Verify document is still in draft before submitting
				try:
					doc_before_submit = api_client.get_document(doctype, document_name)
					if doc_before_submit.get('docstatus', 0) != 0:
						frappe.logger().info(f"[SUBMIT] Document {doctype} {document_name} is no longer in draft (docstatus={doc_before_submit.get('docstatus', 0)}). Already submitted/cancelled.")
						submit_success = True  # Already submitted or cancelled
						break
				except Exception as verify_error:
					frappe.log_error(
						f"[SUBMIT] Could not verify document state before submit attempt {attempt}",
						f"[SUBMIT] Could not verify document state before submit attempt {attempt}: {str(verify_error)}"
					)
				
				submit_result = api_client.submit_document(doctype, document_name)
				frappe.logger().info(f"[SUBMIT] Successfully submitted document {doctype} {document_name} on remote (docstatus=1) on attempt {attempt}")
				submit_success = True
				break
				
			except requests.exceptions.HTTPError as submit_error:
				error_str = str(submit_error)
				
				# Extract detailed error message from response
				error_details = ""
				if submit_error.response:
					try:
						error_response = submit_error.response.json()
						error_details = json.dumps(error_response, indent=2)
						remote_error = error_response.get('exc') or error_response.get('_server_messages') or error_response.get('message', '')
						if remote_error:
							error_str = f"{error_str}\nRemote error: {remote_error}"
					except:
						try:
							error_details = submit_error.response.text[:500]
						except:
							pass
				
				frappe.log_error(
					f"[SUBMIT] Submit attempt {attempt}/{max_retries} failed for {doctype} {document_name}",
					f"[SUBMIT] Submit attempt {attempt}/{max_retries} failed for {doctype} {document_name}: {error_str}"
				)
				
				if attempt < max_retries:
					frappe.logger().info(f"[SUBMIT] Retrying submit in {retry_delay} seconds...")
					time.sleep(retry_delay)
					retry_delay *= 2  # Exponential backoff
				else:
					# Last attempt failed
					# Check if error is because doctype is not submittable on remote
					if "not submittable" in error_str.lower() or "PermissionError" in error_str or "Permission" in error_str:
						frappe.log_error(
							f"Failed to submit document: {doctype} is not submittable on remote",
							(
								f"Could not submit {doctype} {document_name} on remote to set docstatus=1 after {max_retries} attempts.\n\n"
								f"The doctype may not be configured as submittable on the remote server, or there may be a permission issue.\n"
								f"The API user may not have permission to submit documents.\n\n"
								f"Error: {error_str}\n\n"
								f"Error details: {error_details}\n\n"
								f"Document was created as draft (docstatus=0) but could not be submitted."
							)
						)
					else:
						frappe.log_error(
							"Failed to submit document on remote",
							(
								f"Could not submit {doctype} {document_name} on remote to set docstatus=1 after {max_retries} attempts.\n\n"
								f"Error: {error_str}\n\n"
								f"Error details: {error_details}\n\n"
								f"Document was created as draft (docstatus=0) but could not be submitted.\n\n"
								f"This could be due to:\n"
								f"1. Validation errors on the remote document\n"
								f"2. Missing linked documents that need to be synced first\n"
								f"3. Server-side validation rules preventing submission\n"
								f"4. Network or server issues"
							)
						)
			except Exception as submit_error:
				error_str = str(submit_error)
				frappe.log_error(
					f"[SUBMIT] Submit attempt {attempt}/{max_retries} failed with unexpected error",
					f"[SUBMIT] Submit attempt {attempt}/{max_retries} failed with unexpected error: {error_str}"
				)
				
				if attempt < max_retries:
					frappe.logger().info(f"[SUBMIT] Retrying submit in {retry_delay} seconds...")
					time.sleep(retry_delay)
					retry_delay *= 2
				else:
					frappe.log_error(
						"Failed to submit document on remote",
						(
							f"Could not submit {doctype} {document_name} on remote to set docstatus=1 after {max_retries} attempts.\n\n"
							f"Unexpected error: {error_str}\n\n"
							f"Document was created as draft (docstatus=0) but could not be submitted."
						)
					)
		
		if submit_success:
			# Verify the document is now submitted by checking its docstatus
			try:
				submitted_doc = api_client.get_document(doctype, document_name)
				remote_docstatus = submitted_doc.get('docstatus', 0)
				if remote_docstatus == 1:
					frappe.logger().info(f"[SUBMIT] Verified: Document {doctype} {document_name} is now submitted (docstatus=1) on remote")
				else:
					frappe.log_error(
						f"[SUBMIT] Document {doctype} {document_name} submit appeared successful but docstatus is {remote_docstatus}",
						f"[SUBMIT] Document {doctype} {document_name} submit appeared successful but docstatus is {remote_docstatus}, expected 1"
					)
			except Exception as verify_error:
				frappe.log_error(
					f"[SUBMIT] Could not verify docstatus after submit for {doctype} {document_name}",
					f"[SUBMIT] Could not verify docstatus after submit for {doctype} {document_name}: {str(verify_error)}"
				)
	
	except Exception as e:
		frappe.log_error(
			"Failed to submit remote document",
			f"Error in _submit_remote_document for {doctype} {document_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)
		
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


def rename_remote_sales_invoices_by_sync_reference():
	"""
	Cron job to rename Sales Invoices and Payment Entries on remote server where sync_reference != name
	This ensures remote documents match their sync_reference (local name)
	NOTE: This is DISABLED for Sales Invoice and Payment Entry that use naming series,
	as they should keep their remote-generated names and sync_reference should match the local renamed name.
	Runs every 5 minutes via cron
	"""
	try:
		settings = get_sync_settings()
		if not settings or not settings.remote_url or not settings.admin_api_key:
			frappe.log_error(
				"Havano Sync Settings not configured",
				"Havano Sync Settings not configured. Skipping remote document rename."
			)
			return
		
		api_secret = get_decrypted_api_secret(settings)
		if not api_secret:
			frappe.log_error(
				"API Secret not configured",
				"API Secret not configured. Skipping remote document rename."
			)
			return
		
		# Check if naming series is configured for Sales Invoice or Payment Entry
		# If so, skip renaming for these doctypes as they should keep their remote names
		has_sales_invoice_naming_series = hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series
		has_payment_entry_naming_series = hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series
		
		# Initialize API client
		api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
		
		# Process Sales Invoice only if naming series is NOT configured
		if not has_sales_invoice_naming_series:
			_process_remote_rename_by_sync_reference(
				api_client, "Sales Invoice", settings
			)
		else:
			frappe.logger().info("Skipping Sales Invoice rename - naming series is configured")
		
		# Process Payment Entry only if naming series is NOT configured
		if not has_payment_entry_naming_series:
			_process_remote_rename_by_sync_reference(
				api_client, "Payment Entry", settings
			)
		else:
			frappe.logger().info("Skipping Payment Entry rename - naming series is configured")
	
	except Exception as e:
		frappe.log_error(
			"Error in rename_remote_sales_invoices_by_sync_reference",
			f"Unexpected error: {str(e)}"
		)
		frappe.logger().error(f"Unexpected error in rename_remote_sales_invoices_by_sync_reference: {str(e)}")


def _process_remote_rename_by_sync_reference(api_client, doctype: str, settings):
	"""
	Helper function to process renaming remote documents by sync_reference
	"""
	try:
		# Fetch documents from remote where sync_reference != name and sync_type = "Local"
		# These are documents that were synced from local but have different names on remote
		endpoint = "frappe.client.get_list"
		params = {
			"doctype": doctype,
			"filters": json.dumps({
				"sync_type": "Local",
				"sync_reference": ["!=", ""]
			}),
			"fields": json.dumps(["name", "sync_reference"]),
			"limit_page_length": 1000  # Process up to 1000 at a time
		}
		
		documents = api_client._make_request("GET", endpoint, params=params)
		
		if not documents or not isinstance(documents, list):
			frappe.logger().info(f"No {doctype} documents found on remote or invalid response format.")
			return
		
		renamed_count = 0
		error_count = 0
		skipped_count = 0
		
		for doc in documents:
			doc_name = doc.get('name')
			sync_reference = doc.get('sync_reference')
			
			if not doc_name or not sync_reference:
				skipped_count += 1
				continue
			
			# Only rename if sync_reference != name
			if doc_name != sync_reference:
				try:
					frappe.logger().info(f"Renaming remote {doctype} from {doc_name} to {sync_reference}")
					api_client.rename_document(
						doctype=doctype,
						old_name=doc_name,
						new_name=sync_reference,
						force=True,
						merge=False
					)
					renamed_count += 1
					frappe.logger().info(f"Successfully renamed {doctype} from {doc_name} to {sync_reference}")
				except Exception as rename_error:
					error_count += 1
					frappe.log_error(
						f"Failed to rename remote {doctype}",
						f"Could not rename {doctype} from {doc_name} to {sync_reference}: {str(rename_error)}"
					)
			else:
				# sync_reference == name, no rename needed
				skipped_count += 1
		
		if renamed_count > 0 or error_count > 0:
			frappe.logger().info(f"Remote {doctype} rename cron job completed: {renamed_count} renamed, {error_count} errors, {skipped_count} skipped (already matching)")
	
	except Exception as e:
		frappe.log_error(
			f"Error in _process_remote_rename_by_sync_reference for {doctype}",
			f"Error fetching or renaming remote {doctype} documents: {str(e)}"
		)
		frappe.logger().error(f"Error in _process_remote_rename_by_sync_reference for {doctype}: {str(e)}")


def _rename_single_remote_document(doctype: str, remote_name: str, sync_reference: str, settings):
	"""
	Rename a single remote document to match its sync_reference
	"""
	try:
		api_secret = get_decrypted_api_secret(settings)
		if not api_secret:
			frappe.log_error(
				f"[RENAME_REMOTE] API Secret not configured",
				f"[RENAME_REMOTE] API Secret not configured. Cannot rename {doctype} {remote_name}"
			)
			return
		
		api_client = SyncAPI(settings.remote_url, settings.admin_api_key, api_secret)
		
		# Verify document exists on remote
		try:
			remote_doc = api_client.get_document(doctype, remote_name)
			if not remote_doc:
				frappe.log_error(
					f"[RENAME_REMOTE] Document not found on remote",
					f"[RENAME_REMOTE] Document {doctype} {remote_name} not found on remote"
				)
				return
		except Exception as get_error:
			frappe.log_error(
				f"[RENAME_REMOTE] Failed to get document from remote",
				f"[RENAME_REMOTE] Failed to get {doctype} {remote_name} from remote: {str(get_error)}"
			)
			return
		
		# Rename the document
		try:
			api_client.rename_document(
				doctype=doctype,
				old_name=remote_name,
				new_name=sync_reference,
				force=True,
				merge=False
			)
		except Exception as rename_error:
			frappe.log_error(
				f"[RENAME_REMOTE] Failed to rename {doctype}",
				f"[RENAME_REMOTE] Failed to rename {doctype} from {remote_name} to {sync_reference}: {str(rename_error)}"
			)
	except Exception as e:
		frappe.log_error(
			f"[RENAME_REMOTE] Error renaming remote {doctype}",
			f"[RENAME_REMOTE] Error renaming remote {doctype} {remote_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)

