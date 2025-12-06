# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
import time
import requests
from typing import Optional, Dict, Any
from frappe.utils import cint
from havano_sync.havano_sync.utils.sync_api import SyncAPI, DocumentNotFoundError
from havano_sync.havano_sync.tasks.utils import (
    get_sync_settings,
    should_sync_doctype,
    is_submittable_doctype,
    get_decrypted_api_secret,
    belongs_to_company,
    get_syncable_doctypes
)
from havano_sync.havano_sync.tasks.document_preparation import create_minimal_master_document
from havano_sync.havano_sync.tasks.queue import create_sync_log
from havano_sync.havano_sync.tasks.sync_operations import handle_link_validation_error


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
				
				# Note: We don't filter by company here because frappe.client.get_list
				# has security restrictions on which fields can be used in filters.
				# Instead, we filter by company after fetching each document (see line 675).
				# Exception: For Company doctype, we can filter by name
				company = getattr(settings, 'company', None)
				if company and doctype_name == 'Company':
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




