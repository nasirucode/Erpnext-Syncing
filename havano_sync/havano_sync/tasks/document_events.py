# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from typing import Optional
from frappe.model.naming import make_autoname
from havano_sync.havano_sync.tasks.utils import (
    get_sync_settings,
    should_sync_doctype,
    is_submittable_doctype,
    belongs_to_company
)
from havano_sync.havano_sync.tasks.document_preparation import prepare_doc_for_sync
from havano_sync.havano_sync.tasks.queue import queue_sync_job


def set_sync_reference_on_validate(doc, method: Optional[str] = None):
	"""
	Set sync_reference on validate for all documents (lightweight function)
	sync_reference is set to a random number to uniquely identify the document
	"""
	try:
		# Quick skip for DocType and system doctypes
		doctype = doc.doctype
		if doctype == "DocType" or doctype in ("User", "Error Log", "Activity Log", "Comment", "Version", 
			"Communication", "Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type", "Havano Sync Log", "Havano Sync Queue", 
			"Havano Sync Settings", "GL Entry", "Stock Ledger Entry", "Payment Ledger Entry", 
			"Repost Payment Ledger", "Route History", "Webform", "Access Log", "Portal Settings"):
			return
		
		# Only set if sync_reference is not already set
		if not getattr(doc, 'sync_reference', None):
			import random
			import string
			# Generate random alphanumeric string (12 characters)
			sync_reference_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
			doc.sync_reference = sync_reference_value
		
		# Set sync_type to Local if not already set
		if not getattr(doc, 'sync_type', None):
			doc.sync_type = 'Local'
	
	except Exception:
		# Silently fail - don't block document save
		pass


def sync_document_on_create(doc, method: Optional[str] = None):
	"""
	Sync document when it's created
	This is called via doc_events hook
	Only works on Local server to send to Remote
	
	Note: sync_reference is set in validate hook to what the renamed name would have been
	(For most doctypes: name + "-Local", for Sales Invoice/Payment Entry/Quotation: just the name)
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# CRITICAL: Never sync DocType definitions themselves - only document instances
		# (e.g., Error Log, Activity Log, etc.)
		system_doctypes = [
			"DocType",  # Never sync DocType definitions!
			"User",  # Exempt User doctype from syncing
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings",
			"GL Entry", "Stock Ledger Entry", "Payment Ledger Entry", "Repost Payment Ledger",
			"Route History", "Webform", "Access Log", "Portal Settings"
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
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order", "Quotation"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Only sync if it's an auto-sync doctype OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# Check sync_status - skip if already synced or fetched (avoid duplicates)
		from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists, has_sync_status
		ensure_sync_status_field_exists(doctype)
		if has_sync_status(doctype, doc.name):
			return  # Skip if already synced or fetched
		
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
			# This ensures linked documents can reference this document by name
			try:
				# Reload meta to ensure we have the latest fields
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if has_sync_reference and has_sync_type:
					# sync_reference and sync_type are already set in validate hook
					# Just ensure they're persisted if not already set
					current_name = doc.name
					current_sync_ref = frappe.db.get_value(doctype, current_name, 'sync_reference')
					
					# Only update if not already set
					if not current_sync_ref:
						# Generate random sync_reference (same as validate hook)
						import random
						import string
						sync_reference_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
						frappe.db.set_value(doctype, current_name, {
							'sync_reference': sync_reference_value,
							'sync_type': 'Local'
						}, update_modified=False)
						frappe.db.commit()
					else:
						# sync_reference exists, just ensure sync_type is set
						current_sync_type = frappe.db.get_value(doctype, current_name, 'sync_type')
						if not current_sync_type:
							frappe.db.set_value(doctype, current_name, {
								'sync_type': 'Local'
							}, update_modified=False)
							frappe.db.commit()
			except Exception as e:
				frappe.log_error(
					title="Failed to set sync fields on document",
					message=f"Could not set sync_reference and sync_type on {doctype} {doc.name}: {str(e)}"
				)
		
		# Check company filter if specified in settings
		company = getattr(settings, 'company', None)
		if company:
			# Check if document belongs to the specified company
			doc_data_check = doc.as_dict()
			if not belongs_to_company(doc_data_check, doctype, company):
				# Document doesn't belong to the selected company, skip syncing
				return
		
		# Always queue the sync job to avoid delays on save/update/submit
		# Individual documents get higher priority (8) for faster processing
		# This ensures the document name is synced to remote immediately so linked documents can reference it
		doc_data = prepare_doc_for_sync(doc)
		queue_sync_job(
			doctype=doctype,
			name=doc.name,
			sync_type="Send",
			document_data=doc_data,
			priority=8  # Higher priority for individual document syncs
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
	
	IMPORTANT: This function does minimal work to avoid blocking submit.
	All heavy operations are queued to run in background.
	"""
	try:
		doctype = doc.doctype
		
		# Log that hook was triggered
		# frappe.log_error(
		# 	title=f"[SYNC_ON_SUBMIT_HOOK] Triggered for {doctype} {doc.name}",
		# 	message=f"sync_document_on_submit hook called for {doctype} {doc.name}, docstatus={doc.docstatus}"
		# )
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# CRITICAL: Never sync DocType definitions themselves - only document instances
		system_doctypes = [
			"DocType",  # Never sync DocType definitions!
			"User",  # Exempt User doctype from syncing
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings",
			"GL Entry", "Stock Ledger Entry", "Payment Ledger Entry", "Repost Payment Ledger",
			"Route History", "Webform", "Access Log", "Portal Settings"
		]
		
		if doctype in system_doctypes:
			# frappe.log_error(
			# 	title=f"[SYNC_ON_SUBMIT_HOOK] Skipped - system doctype",
			# 	message=f"Skipping {doctype} {doc.name} - system doctype"
			# )
			return
		
		# Only sync submittable doctypes on submit
		if not is_submittable_doctype(doctype):
			# frappe.log_error(
			# 	title=f"[SYNC_ON_SUBMIT_HOOK] Skipped - not submittable",
			# 	message=f"Skipping {doctype} {doc.name} - not a submittable doctype"
			# )
			return
		
		# Only sync if document is actually submitted (docstatus = 1)
		if doc.docstatus != 1:
			# frappe.log_error(
			# 	title=f"[SYNC_ON_SUBMIT_HOOK] Skipped - not submitted",
			# 	message=f"Skipping {doctype} {doc.name} - docstatus={doc.docstatus}, not submitted (1)"
			# )
			return
		
		# Store document info before queuing background job
		document_name = doc.name
		
		# frappe.log_error(
		# 	title=f"[SYNC_ON_SUBMIT_HOOK] Enqueuing background job",
		# 	message=f"Enqueuing _process_sync_on_submit for {doctype} {document_name}"
		# )
		
		# Queue ALL processing in background - this ensures submit doesn't block at all
		# All checks, field setup, and sync will happen in background
		frappe.enqueue(
			_process_sync_on_submit,
			doctype=doctype,
			document_name=document_name,
			queue="short",
			timeout=300,
			is_async=True,
			job_name=f"sync_on_submit_{doctype}_{document_name}"
		)
		
		# frappe.log_error(
		# 	title=f"[SYNC_ON_SUBMIT_HOOK] Background job enqueued",
		# 	message=f"Successfully enqueued _process_sync_on_submit for {doctype} {document_name}"
		# )
	
	except Exception as e:
		frappe.log_error(
			title=f"Sync on Submit Failed: {doc.doctype} {doc.name}",
			message=f"Error in sync_document_on_submit: {str(e)}\n{frappe.get_traceback()}"
		)




def sync_document_on_update(doc, method: Optional[str] = None):
	"""
	Sync document when it's updated
	This is called via doc_events hook
	Only works on Local server to send to Remote
	For syncable doctypes with 'send' enabled, auto-syncs on save
	
	IMPORTANT: This function does minimal work to avoid blocking update.
	All processing is queued to run in background.
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# CRITICAL: Never sync DocType definitions themselves - only document instances
		system_doctypes = [
			"DocType",  # Never sync DocType definitions!
			"User",  # Exempt User doctype from syncing
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings",
			"GL Entry", "Stock Ledger Entry", "Payment Ledger Entry", "Repost Payment Ledger",
			"Route History", "Webform", "Access Log", "Portal Settings"
		]
		
		if doctype in system_doctypes:
			return
		
		# For submittable doctypes:
		# - Skip entirely in on_update because on_submit will handle it
		# - This prevents double syncing when a document is submitted
		#   (both on_submit and on_update fire during submit)
		if is_submittable_doctype(doctype):
			# Skip on_update for submittable doctypes - on_submit hook will handle syncing
			return
		
		# Store document info before queuing background job
		document_name = doc.name
		
		# Queue ALL processing in background - this ensures update doesn't block at all
		frappe.enqueue(
			_process_sync_on_update,
			doctype=doctype,
			document_name=document_name,
			queue="short",
			timeout=300,
			is_async=True,
			job_name=f"sync_on_update_{doctype}_{document_name}"
		)
	
	except Exception as e:
		frappe.log_error(
			f"Sync on Update Failed: {doc.doctype} {doc.name}",
			frappe.get_traceback()
		)


def add_local_suffix_after_insert(doc, method: Optional[str] = None):
	"""
	DEPRECATED: -Local suffix renaming has been removed.
	This function is kept for backward compatibility but does nothing.
	"""
	pass


def _process_sync_on_submit(doctype: str, document_name: str):
	"""
	Process sync on submit in background after submit completes
	This function runs ALL checks and operations in background to avoid blocking submit
	"""
	"""
	Process sync on submit in background after submit completes
	This function runs ALL checks and operations in background to avoid blocking submit
	"""
	try:
		# frappe.log_error(
		# 	title=f"[SYNC_ON_SUBMIT] Processing {doctype} {document_name}",
		# 	message=f"Processing {doctype} {document_name} in background"
		# )
		
		# Get settings in background
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return
		
		# Auto-sync doctypes that should always sync (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order", "Quotation"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists, has_sync_status, should_sync_doctype
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Only sync if it's in auto-sync doctypes OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# Check sync_status - skip if already synced or fetched (avoid duplicates)
		# For auto-sync doctypes, we still check sync_status to avoid duplicates
		# But we don't skip if sync_status is "Pending" or empty
		ensure_sync_status_field_exists(doctype)
		if has_sync_status(doctype, document_name):
			# Document already synced or fetched, skip
			return
		
		# Get document to check company and prepare for sync
		actual_document_name = document_name
		doc = None
		try:
			doc = frappe.get_doc(doctype, document_name)
		except frappe.DoesNotExistError:
			# Document doesn't exist
			frappe.log_error(
				title=f"[SYNC_ON_SUBMIT] Document {doctype} {document_name} does not exist",
				message=f"[SYNC_ON_SUBMIT] Document {doctype} {document_name} does not exist"
			)
			return
		
		# Ensure we have a valid doc object
		if not doc:
			# frappe.log_error(
			# 	title=f"[SYNC_ON_SUBMIT] Could not load document {doctype} {actual_document_name}",
			# 	message=f"[SYNC_ON_SUBMIT] Could not load document {doctype} {actual_document_name}"
			# )
			return
		
		# Check company filter if specified in settings
		company = getattr(settings, 'company', None)
		if company:
			doc_data_check = doc.as_dict()
			if not belongs_to_company(doc_data_check, doctype, company):
				# Document doesn't belong to the selected company, skip syncing
				return
		
		# Ensure sync fields exist and set them (in background)
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
							"unique": 1
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
					frappe.clear_cache(doctype=doctype)
					frappe.clear_cache()
					meta = frappe.get_meta(doctype)
			except Exception as e:
				frappe.log_error(
					title="Failed to ensure sync fields locally",
					message=f"Could not ensure sync fields exist locally for {doctype}: {str(e)}"
				)
			
			# Set sync_reference and sync_type on the document
			try:
				meta = frappe.get_meta(doctype)
				has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
				has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
				
				if has_sync_reference and has_sync_type:
					# sync_reference is already set in validate hook as random number, just ensure sync_type is set
					current_sync_ref = frappe.db.get_value(doctype, actual_document_name, 'sync_reference')
					if not current_sync_ref:
						# Generate random sync_reference if not set
						import random
						import string
						sync_reference_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
						frappe.db.set_value(doctype, actual_document_name, {
							'sync_reference': sync_reference_value,
							'sync_type': 'Local'
						}, update_modified=False)
						frappe.db.commit()

					else:
						# sync_reference exists, just ensure sync_type is set
						current_sync_type = frappe.db.get_value(doctype, actual_document_name, 'sync_type')
						if not current_sync_type:
							frappe.db.set_value(doctype, actual_document_name, {
								'sync_type': 'Local'
							}, update_modified=False)
							frappe.db.commit()
			except Exception as e:
				frappe.log_error(
					title="Failed to set sync fields on document",
					message=f"Could not set sync_reference and sync_type on {doctype} {document_name}: {str(e)}"
				)
		
		# Set sync_status to 'Pending' before queuing
		# This ensures the cron job can pick up these documents
		try:
			from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists
			ensure_sync_status_field_exists(doctype)
			if frappe.db.has_column(doctype, 'sync_status'):
				frappe.db.set_value(doctype, actual_document_name, 'sync_status', 'Pending', update_modified=False)
				frappe.db.commit()
		except Exception as e:
			frappe.log_error(
				title="Failed to set sync_status to Pending",
				message=f"Could not set sync_status='Pending' for {doctype} {actual_document_name}: {str(e)}"
			)
		
		# Queue sync job in background
		# Use actual_document_name for syncing (no renaming anymore)
		doc_data = prepare_doc_for_sync(doc)
		# For Sales Invoice, Payment Entry, and Quotation, skip naming series check
		skip_naming_check = doctype in ("Sales Invoice", "Payment Entry", "Quotation")
		queue_sync_job(
			doctype=doctype,
			name=actual_document_name,
			sync_type="Send",
			document_data=doc_data,
			priority=8  # Higher priority for individual document syncs
		)
		# frappe.log_error(
		# 	title=f"[SYNC_ON_SUBMIT] Queued sync for {doctype} {actual_document_name}",
		# 	message=f"Queued sync for {doctype} {actual_document_name}"
		# )
		
	except Exception as e:
		frappe.log_error(
			title="Process Sync On Submit Failed",
			message=f"Error in _process_sync_on_submit for {doctype} {document_name} (actual: {actual_document_name if 'actual_document_name' in locals() else 'N/A'}): {str(e)}\nTraceback: {frappe.get_traceback()}"
		)


def _process_sync_on_update(doctype: str, document_name: str):
	"""
	Process sync on update in background after update completes
	This function runs ALL checks and operations in background to avoid blocking update
	"""
	try:
		# Import required functions at the start
		from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists, has_sync_status, should_sync_doctype
		
		frappe.logger().info(f"[SYNC_ON_UPDATE] Processing {doctype} {document_name} in background")
		
		# Get settings in background
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return
		
		# Check if this doctype is enabled for sending
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Auto-sync doctypes that should always sync on update (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order", "Quotation"}
		should_auto_sync = doctype in auto_sync_doctypes
		
		# Only sync if it's in auto-sync doctypes OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# Check sync_status - skip if already synced or fetched (avoid duplicates)
		if should_sync_doctype(doctype, settings, direction="send"):
			ensure_sync_status_field_exists(doctype)
			if has_sync_status(doctype, document_name):
				return  # Skip if already synced or fetched
		
		# Get document to check company and prepare for sync
		try:
			doc = frappe.get_doc(doctype, document_name)
		except frappe.DoesNotExistError:
			frappe.log_error(
				title=f"[SYNC_ON_UPDATE] Document {doctype} {document_name} does not exist",
				message=f"[SYNC_ON_UPDATE] Document {doctype} {document_name} does not exist"
			)
			return
		
		# Check company filter if specified in settings
		company = getattr(settings, 'company', None)
		if company:
			doc_data_check = doc.as_dict()
			if not belongs_to_company(doc_data_check, doctype, company):
				# Document doesn't belong to the selected company, skip syncing
				return
		
		# Directly sync instead of queuing another job (we're already in a background job)
		# This prevents duplicate queue entries
		from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
		doc_data = prepare_doc_for_sync(doc)
		sync_document_to_remote(
			doctype=doctype,
			name=document_name,
			target_url=settings.remote_url,
			api_key=settings.admin_api_key,
			api_secret=None,  # Will be decrypted in function
			force_create=False,
			sync_method="Auto",
			settings=settings
		)
		frappe.logger().info(f"[SYNC_ON_UPDATE] Synced {doctype} {document_name}")
		
	except Exception as e:
		frappe.log_error(
			title="Process Sync On Update Failed",
			message=f"Error in _process_sync_on_update for {doctype} {document_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)




def _queue_sync_if_needed(doctype: str, document_name: str):
	"""
	Queue sync if document should be synced
	Uses sync_status to avoid duplicates
	"""
	try:
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return
		
		# Auto-sync doctypes that should always sync (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order", "Quotation"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Only sync if it's an auto-sync doctype OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# Check sync_status - skip if already synced or fetched (avoid duplicates)
		from havano_sync.havano_sync.tasks.utils import ensure_sync_status_field_exists, has_sync_status
		ensure_sync_status_field_exists(doctype)
		if has_sync_status(doctype, document_name):
			return  # Skip if already synced or fetched
		
		# Get document to check company
		# For Sales Invoice, Payment Entry, and Quotation, the document may have been renamed with naming series
		actual_document_name = document_name
		doc = None
		try:
			doc = frappe.get_doc(doctype, document_name)
		except frappe.DoesNotExistError:
			# If document doesn't exist, wait a moment in case rename is still in progress
			import time
			time.sleep(0.5)
			try:
				doc = frappe.get_doc(doctype, document_name)
			except frappe.DoesNotExistError:
				# Document doesn't exist with the given name
				# For Sales Invoice, Payment Entry, and Quotation with naming series, it may have been renamed
				if doctype in ("Sales Invoice", "Payment Entry", "Quotation"):
					# Try to find the renamed document by checking for documents with naming series pattern
					if settings:
						try:
							naming_series_to_check = None
							if doctype == "Payment Entry" and hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series:
								naming_series_to_check = settings.payment_entry_naming_series
							elif doctype == "Sales Invoice" and hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series:
								naming_series_to_check = settings.sales_invoice_naming_series
							elif doctype == "Quotation" and hasattr(settings, 'quotation_naming_series') and settings.quotation_naming_series:
								naming_series_to_check = settings.quotation_naming_series
							
							if naming_series_to_check:
								# Try to find document by checking recent documents with the naming series
								import re
								pattern_match = re.match(r'^([A-Z0-9\-]+)', naming_series_to_check)
								if pattern_match:
									prefix = pattern_match.group(1).rstrip('-')
									table_name = f"tab{doctype}"
									recent_docs = frappe.db.sql(f"""
										SELECT name FROM `{table_name}`
										WHERE name LIKE %s
										AND name != %s
										AND sync_type = 'Local'
										AND creation >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
										ORDER BY creation DESC
										LIMIT 1
									""", (prefix + '%', document_name), as_dict=True)
									
									if recent_docs:
										actual_document_name = recent_docs[0].name
										try:
											doc = frappe.get_doc(doctype, actual_document_name)
											frappe.log_error(
												title=f"[QUEUE_SYNC] Found renamed document {doctype} {actual_document_name}",
												message=f"Found renamed document {doctype} {actual_document_name} by pattern matching (was {document_name})"
											)
										except frappe.DoesNotExistError:
											frappe.log_error(
												title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist",
												message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist and could not find renamed version"
											)
											return
									else:
										frappe.log_error(
											title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist",
											message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist and could not find renamed version (no recent documents with pattern {prefix}%)"
										)
										return
								else:
									frappe.log_error(
										title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist",
										message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist and could not parse naming series pattern"
									)
									return
							else:
								frappe.log_error(
									title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist (no naming series configured)",
									message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist (no naming series configured)"
								)
								return
						except Exception as find_error:
							frappe.log_error(
								title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist and error finding renamed version",
								message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist and error finding renamed version: {str(find_error)}"
							)
							return
					else:
						frappe.log_error(
							title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist (no settings)",
							message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist (no settings)"
						)
						return
				else:
					frappe.log_error(
						title=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist",
						message=f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist"
					)
					return
		
		# Ensure we have a valid doc object
		if not doc:
			frappe.log_error(
				title=f"[QUEUE_SYNC] Could not load document {doctype} {actual_document_name}",
				message=f"[QUEUE_SYNC] Could not load document {doctype} {actual_document_name}"
			)
			return
		
		# Check company filter if specified in settings
		company = getattr(settings, 'company', None)
		if company:
			doc_data_check = doc.as_dict()
			if not belongs_to_company(doc_data_check, doctype, company):
				# Document doesn't belong to the selected company, skip
				frappe.logger().info(f"[QUEUE_SYNC] Document {doctype} {document_name} does not belong to company {company}, skipping")
				return
		
		# Ensure sync fields exist and set them before queuing sync
		try:
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
						"unique": 1
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
				frappe.clear_cache(doctype=doctype)
				frappe.clear_cache()
				meta = frappe.get_meta(doctype)
			
			# Set sync_reference and sync_type on the document
			meta = frappe.get_meta(doctype)
			has_sync_reference = any(f.fieldname == 'sync_reference' for f in meta.fields)
			has_sync_type = any(f.fieldname == 'sync_type' for f in meta.fields)
			
			if has_sync_reference and has_sync_type:
				# sync_reference is already set in validate hook, just ensure sync_type is set
				current_sync_ref = frappe.db.get_value(doctype, actual_document_name, 'sync_reference')
				if not current_sync_ref:
					# Generate random sync_reference if not set
					import random
					import string
					sync_reference_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
					frappe.db.set_value(doctype, actual_document_name, {
						'sync_reference': sync_reference_value,
						'sync_type': 'Local'
					}, update_modified=False)
					frappe.db.commit()
					frappe.logger().info(f"[QUEUE_SYNC] Set sync_reference={sync_reference_value} and sync_type=Local on {doctype} {actual_document_name}")
				else:
					# sync_reference exists, just ensure sync_type is set
					current_sync_type = frappe.db.get_value(doctype, actual_document_name, 'sync_type')
					if not current_sync_type:
						frappe.db.set_value(doctype, actual_document_name, {
							'sync_type': 'Local'
						}, update_modified=False)
						frappe.db.commit()
						frappe.logger().info(f"[QUEUE_SYNC] Set sync_type=Local on {doctype} {actual_document_name}")
		except Exception as e:
			frappe.log_error(
				title="Failed to ensure sync fields before queuing sync",
				message=f"Could not ensure sync fields for {doctype} {document_name}: {str(e)}"
			)
			# Continue anyway - try to queue sync
		
		# Queue sync job
		# Use actual_document_name (may be renamed) for queuing
		doc_data = prepare_doc_for_sync(doc)
		queue_sync_job(
			doctype=doctype,
			name=actual_document_name,
			sync_type="Send",
			document_data=doc_data,
			priority=8  # Higher priority for individual document syncs
		)
		
	except Exception as e:
		frappe.log_error(
			title="Failed to queue sync after rename",
			message=f"Could not queue sync for {doctype} {document_name} (actual: {actual_document_name if 'actual_document_name' in locals() else 'N/A'}): {str(e)}\nTraceback: {frappe.get_traceback()}"
		)








