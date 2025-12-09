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


def sync_document_on_create(doc, method: Optional[str] = None):
	"""
	Sync document when it's created
	This is called via doc_events hook
	Only works on Local server to send to Remote
	
	Note: For documents that get renamed with -Local suffix, sync is triggered
	after the rename in the after_commit callback to ensure the correct name is synced.
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# CRITICAL: Never sync DocType definitions themselves - only document instances
		# (e.g., Error Log, Activity Log, etc.)
		system_doctypes = [
			"DocType",  # Never sync DocType definitions!
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
		
		# Check if document name already has -Local suffix
		# If it does, it means rename already happened, so we can sync now
		# If it doesn't, the rename will happen in after_commit and sync will be triggered there
		# This prevents double syncing
		if not doc.name.endswith("-Local"):
			# Document will be renamed in after_commit, sync will be triggered there
			# Skip syncing here to avoid syncing with wrong name
			frappe.logger().debug(f"Skipping sync for {doctype} {doc.name} - will sync after rename to {doc.name}-Local")
			return
		
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
			
			# Note: The -Local suffix is now added by add_local_suffix_after_insert function
			# which runs for all doctypes including submittable ones
			# This ensures all documents get the -Local suffix
			# Reload the document to get the updated name (with -Local suffix if it was renamed)
			try:
				doc.reload()
			except:
				pass
			
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
					# Set the fields using db_set to avoid triggering hooks
					# Use the current document name (which may have -Local suffix)
					current_name = doc.name
					frappe.db.set_value(doctype, current_name, {
						'sync_reference': current_name,
						'sync_type': 'Local'
					}, update_modified=False)
					frappe.db.commit()
					frappe.logger().info(f"Set sync_reference={current_name} and sync_type=Local on {doctype} {current_name} (on after_insert)")
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
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# CRITICAL: Never sync DocType definitions themselves - only document instances
		system_doctypes = [
			"DocType",  # Never sync DocType definitions!
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
		
		# Store document info before queuing background job
		document_name = doc.name
		
		# For Sales Invoice and Payment Entry, check if naming series is configured
		# If so, queue rename with naming series before syncing
		if doctype in ("Sales Invoice", "Payment Entry"):
			settings = get_sync_settings()
			if settings:
				naming_series_to_use = None
				if doctype == "Payment Entry" and hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series:
					naming_series_to_use = settings.payment_entry_naming_series
				elif doctype == "Sales Invoice" and hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series:
					naming_series_to_use = settings.sales_invoice_naming_series
				
				if naming_series_to_use:
					# Queue rename with naming series, then sync
					frappe.enqueue(
						_rename_with_naming_series_on_submit,
						doctype=doctype,
						document_name=document_name,
						naming_series=naming_series_to_use,
						queue="short",
						timeout=300,
						is_async=True,
						job_name=f"rename_with_naming_series_{doctype}_{document_name}"
					)
					return  # Exit early, rename function will queue sync
		
		# Check if document has -Local suffix, if not, log a warning
		# Note: We can't rename it now because it's already submitted
		if not document_name.endswith("-Local"):
			frappe.logger().warning(
				f"[SYNC_ON_SUBMIT] Document {doctype} {document_name} was submitted without -Local suffix. "
				f"The rename may not have completed yet. Sync will proceed with current name."
			)
		
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
	
	IMPORTANT: This function does minimal work to avoid blocking update.
	All processing is queued to run in background.
	"""
	try:
		doctype = doc.doctype
		
		# Prevent syncing system/internal doctypes to avoid recursion
		# CRITICAL: Never sync DocType definitions themselves - only document instances
		system_doctypes = [
			"DocType",  # Never sync DocType definitions!
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings"
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
	Add "-Local" suffix to document name after insert for all syncable and compulsory doctypes
	This runs for ALL documents including submittable ones
	
	IMPORTANT: This function does minimal work to avoid blocking save.
	Rename happens after commit (non-blocking), sync is queued in background.
	
	Flow:
	1. Save completes (after_insert hook fires)
	2. Minimal checks only (no heavy operations)
	3. Register rename callback to run after commit (non-blocking)
	4. After rename, sync will be queued in background
	"""
	# CRITICAL: Never rename DocType definitions themselves - only document instances
	# This MUST be the absolute first check - before any other code
	if not hasattr(doc, 'doctype') or doc.doctype == "DocType":
		return
	
	try:
		doctype = doc.doctype
		
		# Prevent processing system/internal doctypes to avoid recursion
		system_doctypes = [
			"Error Log", "Activity Log", "Comment", "Version", "Communication",
			"Email Queue", "Email Queue Recipient", "Notification Log",
			"Scheduled Job Log", "Scheduled Job Type",
			"Havano Sync Log", "Havano Sync Queue", "Havano Sync Settings"
		]
		
		if doctype in system_doctypes:
			return
		
		# Store document info
		if not hasattr(doc, 'name') or not doc.name:
			return
		
		original_name = doc.name
		
		# Check if document has sync_type="Remote" - if so, do not rename with -Local suffix
		# Documents fetched from remote should keep their original name
		try:
			sync_type = getattr(doc, 'sync_type', None)
			if sync_type == "Remote":
				# Document is from remote, do not rename - keep original name
				frappe.logger().info(f"[ADD_LOCAL_SUFFIX] Document {doctype} {original_name} has sync_type=Remote, skipping -Local suffix")
				# Still queue sync in case it needs to be synced back
				frappe.enqueue(
					_queue_sync_if_needed,
					doctype=doctype,
					document_name=original_name,
					queue="short",
					timeout=300,
					is_async=True,
					job_name=f"queue_sync_{doctype}_{original_name}"
				)
				return
		except Exception:
			# If we can't check sync_type, continue with normal flow
			pass
		
		# Check if name already has -Local suffix
		if original_name.endswith("-Local"):
			# Already renamed, just queue sync in background
			frappe.enqueue(
				_queue_sync_if_needed,
				doctype=doctype,
				document_name=original_name,
				queue="short",
				timeout=300,
				is_async=True,
				job_name=f"queue_sync_{doctype}_{original_name}"
			)
			return
		
		# For Sales Invoice and Payment Entry, check if naming series is configured
		# If so, skip adding -Local suffix - the naming series will be used instead
		if doctype in ("Sales Invoice", "Payment Entry"):
			try:
				settings = get_sync_settings()
				if settings:
					if doctype == "Payment Entry" and hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series:
						# Naming series configured, skip -Local suffix
						# The document will be renamed with naming series in sync_operations.py
						frappe.enqueue(
							_queue_sync_if_needed,
							doctype=doctype,
							document_name=original_name,
							queue="short",
							timeout=300,
							is_async=True,
							job_name=f"queue_sync_{doctype}_{original_name}"
						)
						return
					elif doctype == "Sales Invoice" and hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series:
						# Naming series configured, skip -Local suffix
						# The document will be renamed with naming series in sync_operations.py
						frappe.enqueue(
							_queue_sync_if_needed,
							doctype=doctype,
							document_name=original_name,
							queue="short",
							timeout=300,
							is_async=True,
							job_name=f"queue_sync_{doctype}_{original_name}"
						)
						return
			except Exception:
				# If we can't check settings, continue with -Local suffix
				pass
		
		# Prepare new name
		new_name = f"{original_name}-Local"
		
		# Check if the new name already exists
		if frappe.db.exists(doctype, new_name):
			frappe.logger().warning(f"[ADD_LOCAL_SUFFIX] Document name {new_name} already exists, keeping original name {original_name}")
			# Queue sync with original name in background
			frappe.enqueue(
				_queue_sync_if_needed,
				doctype=doctype,
				document_name=original_name,
				queue="short",
				timeout=300,
				is_async=True,
				job_name=f"queue_sync_{doctype}_{original_name}"
			)
			return
		
		# For submittable doctypes, rename with minimal delay (1 second) to prevent submission before rename
		# For non-submittable doctypes, use longer delay (10 seconds)
		delay_seconds = 1 if is_submittable_doctype(doctype) else 10
		rename_function = _rename_document_immediately if is_submittable_doctype(doctype) else _rename_document_after_delay
		
		# Queue rename job in background - this ensures save is not delayed at all
		frappe.enqueue(
			rename_function,
			doctype=doctype,
			original_name=original_name,
			new_name=new_name,
			delay_seconds=delay_seconds,
			queue="short",
			timeout=300,
			is_async=True,
			job_name=f"rename_{doctype}_{original_name}"
		)
		
	except Exception as e:
		frappe.log_error(
			f"Add Local Suffix After Insert Failed: {doc.doctype} {doc.name if hasattr(doc, 'name') else 'new document'}",
			frappe.get_traceback()
		)


def _rename_document_immediately(doctype: str, original_name: str, new_name: str, delay_seconds: int = 1):
	"""
	Rename document after a short delay (default 1 second)
	This is used for submittable doctypes to ensure rename happens before submission
	"""
	try:
		# CRITICAL: Never rename DocType definitions themselves - only document instances
		if doctype == "DocType":
			return
		
		# Wait a short time before renaming to ensure document is fully saved
		import time
		frappe.logger().info(f"[RENAME_IMMEDIATELY] Waiting {delay_seconds} second(s) before renaming {doctype} {original_name}")
		time.sleep(delay_seconds)
		frappe.logger().info(f"[RENAME_IMMEDIATELY] {delay_seconds} second(s) elapsed, proceeding with rename for {doctype} {original_name}")
		
		# Verify document exists
		if not frappe.db.exists(doctype, original_name):
			frappe.logger().warning(f"[RENAME_IMMEDIATELY] Document {doctype} {original_name} does not exist")
			return
		
		# Check if document is submitted - if so, we can't rename it
		try:
			doc = frappe.get_doc(doctype, original_name)
			if is_submittable_doctype(doctype) and doc.docstatus == 1:
				frappe.logger().info(f"[RENAME_IMMEDIATELY] Document {doctype} {original_name} is already submitted, cannot rename. Proceeding with sync using original name.")
				# Queue sync with original name (without -Local suffix)
				_queue_sync_if_needed(doctype, original_name)
				return
		except Exception as check_error:
			# If we can't check docstatus, continue with rename attempt
			frappe.logger().warning(f"[RENAME_IMMEDIATELY] Could not check docstatus for {doctype} {original_name}: {str(check_error)}")
		
		# Check if new name still available
		if frappe.db.exists(doctype, new_name):
			frappe.logger().warning(f"[RENAME_IMMEDIATELY] Document name {new_name} already exists, cannot rename")
			# Queue sync with original name
			_queue_sync_if_needed(doctype, original_name)
			return
		
		# Rename the document
		try:
			frappe.rename_doc(doctype, original_name, new_name, force=True, merge=False, show_alert=False)
			frappe.logger().info(f"[RENAME_IMMEDIATELY] Renamed {doctype} from {original_name} to {new_name}")
			
			# Verify rename succeeded - wait a moment and verify document exists with new name
			import time
			time.sleep(0.5)  # Small delay to ensure rename is committed
			
			# Double-check rename succeeded
			if not frappe.db.exists(doctype, new_name):
				frappe.logger().error(f"[RENAME_IMMEDIATELY] Rename verification failed - {new_name} does not exist after rename")
				return  # Don't sync if rename verification failed
			
			# Verify original name no longer exists
			if frappe.db.exists(doctype, original_name):
				frappe.logger().warning(f"[RENAME_IMMEDIATELY] Original name {original_name} still exists after rename. Waiting and rechecking.")
				time.sleep(1)  # Wait a bit more
				if frappe.db.exists(doctype, original_name):
					frappe.logger().error(f"[RENAME_IMMEDIATELY] Rename incomplete - original name still exists")
					return  # Don't sync if rename is incomplete
			
			# Rename is complete and verified - sync with new name
			frappe.logger().info(f"[RENAME_IMMEDIATELY] Rename verified complete. Syncing {doctype} {new_name}")
			_queue_sync_if_needed(doctype, new_name)
		except Exception as rename_error:
			# Check if error is due to document being submitted
			error_msg = str(rename_error)
			if "submitted" in error_msg.lower() or "docstatus" in error_msg.lower():
				frappe.logger().info(f"[RENAME_IMMEDIATELY] Cannot rename submitted document {doctype} {original_name}. Proceeding with sync using original name.")
				# Queue sync with original name (without -Local suffix)
				_queue_sync_if_needed(doctype, original_name)
			else:
				frappe.log_error(
					title="Failed to rename document with -Local suffix",
					message=f"Could not rename {doctype} {original_name} to {new_name}: {str(rename_error)}\nTraceback: {frappe.get_traceback()}"
				)
				# Try to sync with original name if rename failed
				_queue_sync_if_needed(doctype, original_name)
	except Exception as e:
		frappe.log_error(
			title="Failed to rename document with -Local suffix",
			message=f"Could not rename {doctype} {original_name} to {new_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)
		# Try to sync with original name if rename failed
		_queue_sync_if_needed(doctype, original_name)


def _rename_document_after_delay(doctype: str, original_name: str, new_name: str, delay_seconds: int = 10):
	"""
	Rename document after delay (default 10 seconds)
	This runs in a background job, so it doesn't block the save process at all
	"""
	try:
		# CRITICAL: Never rename DocType definitions themselves - only document instances
		if doctype == "DocType":
			return
		
		# Wait before renaming to ensure document is fully saved
		import time
		frappe.logger().info(f"[RENAME_AFTER_DELAY] Waiting {delay_seconds} seconds before renaming {doctype} {original_name}")
		time.sleep(delay_seconds)
		frappe.logger().info(f"[RENAME_AFTER_DELAY] {delay_seconds} seconds elapsed, proceeding with rename for {doctype} {original_name}")
		
		# Verify document still exists
		if not frappe.db.exists(doctype, original_name):
			frappe.logger().warning(f"[RENAME_AFTER_DELAY] Document {doctype} {original_name} does not exist")
			return
		
		# Check if document is submitted - if so, we can't rename it
		# For submittable doctypes, if they're submitted before rename, skip rename but proceed with sync
		try:
			doc = frappe.get_doc(doctype, original_name)
			if is_submittable_doctype(doctype) and doc.docstatus == 1:
				frappe.logger().info(f"[RENAME_AFTER_DELAY] Document {doctype} {original_name} is already submitted, cannot rename. Proceeding with sync using original name.")
				# Queue sync with original name (without -Local suffix)
				_queue_sync_if_needed(doctype, original_name)
				return
		except Exception as check_error:
			# If we can't check docstatus, continue with rename attempt
			frappe.logger().warning(f"[RENAME_AFTER_DELAY] Could not check docstatus for {doctype} {original_name}: {str(check_error)}")
		
		# Check if new name still available
		if frappe.db.exists(doctype, new_name):
			frappe.logger().warning(f"[RENAME_AFTER_DELAY] Document name {new_name} already exists, cannot rename")
			# Queue sync with original name
			_queue_sync_if_needed(doctype, original_name)
			return
		
		# Rename the document
		try:
			frappe.rename_doc(doctype, original_name, new_name, force=True, merge=False, show_alert=False)
			frappe.logger().info(f"[RENAME_AFTER_DELAY] Renamed {doctype} from {original_name} to {new_name}")
			
			# Verify rename succeeded - wait a moment and verify document exists with new name
			import time
			time.sleep(0.5)  # Small delay to ensure rename is committed
			
			# Double-check rename succeeded
			if not frappe.db.exists(doctype, new_name):
				frappe.logger().error(f"[RENAME_AFTER_DELAY] Rename verification failed - {new_name} does not exist after rename")
				return  # Don't sync if rename verification failed
			
			# Verify original name no longer exists
			if frappe.db.exists(doctype, original_name):
				frappe.logger().warning(f"[RENAME_AFTER_DELAY] Original name {original_name} still exists after rename. Waiting and rechecking.")
				time.sleep(1)  # Wait a bit more
				if frappe.db.exists(doctype, original_name):
					frappe.logger().error(f"[RENAME_AFTER_DELAY] Rename incomplete - original name still exists")
					return  # Don't sync if rename is incomplete
			
			# Rename is complete and verified - sync with new name
			frappe.logger().info(f"[RENAME_AFTER_DELAY] Rename verified complete. Syncing {doctype} {new_name}")
			_queue_sync_if_needed(doctype, new_name)
		except Exception as rename_error:
			# Check if error is due to document being submitted
			error_msg = str(rename_error)
			if "submitted" in error_msg.lower() or "docstatus" in error_msg.lower():
				frappe.logger().info(f"[RENAME_AFTER_DELAY] Cannot rename submitted document {doctype} {original_name}. Proceeding with sync using original name.")
				# Queue sync with original name (without -Local suffix)
				_queue_sync_if_needed(doctype, original_name)
				return
			else:
				# Re-raise other errors
				raise
	except Exception as e:
		frappe.log_error(
			title="Failed to rename document with -Local suffix",
			message=f"Could not rename {doctype} {original_name} to {new_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)
		# Try to sync with original name if rename failed
		_queue_sync_if_needed(doctype, original_name)



def _rename_with_naming_series_on_submit(doctype: str, document_name: str, naming_series: str):
	"""
	Rename Sales Invoice or Payment Entry with naming series on submit, then sync
	This runs in background after submit completes
	"""
	try:
		frappe.logger().info(f"[RENAME_WITH_NAMING_SERIES] Processing {doctype} {document_name} with naming series {naming_series}")
		
		# Verify document exists
		if not frappe.db.exists(doctype, document_name):
			frappe.logger().warning(f"[RENAME_WITH_NAMING_SERIES] Document {doctype} {document_name} does not exist")
			return
		
		# Get the document
		doc = frappe.get_doc(doctype, document_name)
		
		# Check if document already has the correct naming series
		current_naming_series = doc.get('naming_series', '')
		if current_naming_series == naming_series:
			# Already has correct naming series, generate new name
			original_naming_series = doc.naming_series
			doc.naming_series = naming_series
			new_name = make_autoname(naming_series, doctype, doc)
			doc.naming_series = original_naming_series
		else:
			# Set naming series and generate new name
			original_naming_series = doc.naming_series
			doc.naming_series = naming_series
			new_name = make_autoname(naming_series, doctype, doc)
			doc.naming_series = original_naming_series
		
		# Check if new name is different from current name
		if new_name and new_name != document_name:
			# Check if new name already exists
			if frappe.db.exists(doctype, new_name):
				frappe.logger().warning(f"[RENAME_WITH_NAMING_SERIES] Document name {new_name} already exists, cannot rename {doctype} {document_name}. Rename failed, not syncing.")
				return  # Don't sync if rename failed
			
			# Try to rename (even if submitted, with force=True)
			try:
				# IMPORTANT: For submitted documents, we need to set sync_reference BEFORE rename
				# because Frappe doesn't allow changing sync_reference after submission
				if doc.docstatus == 1 and frappe.db.has_column(doctype, 'sync_reference'):
					try:
						frappe.db.sql(
							f"UPDATE `tab{doctype}` SET sync_reference = %s, sync_type = 'Local' WHERE name = %s",
							(new_name, document_name)
						)
						frappe.db.commit()
						frappe.logger().info(f"[RENAME_WITH_NAMING_SERIES] Set sync_reference={new_name} before rename for submitted document")
					except Exception as sync_ref_error:
						frappe.log_error(
							title="Failed to set sync_reference before rename",
							message=f"Could not set sync_reference before renaming {doctype} {document_name}: {str(sync_ref_error)}"
						)
						# Continue with rename anyway
				
				# Rename the document
				# Note: Frappe may show "Value cannot be changed for Series" error in logs,
				# but the rename will still succeed if force=True
				frappe.rename_doc(doctype, document_name, new_name, force=True, merge=False, show_alert=False)
				frappe.logger().info(f"[RENAME_WITH_NAMING_SERIES] Renamed {doctype} from {document_name} to {new_name}")
				
				# Wait a moment and verify rename succeeded
				import time
				time.sleep(0.5)
				
				# Verify rename succeeded by checking if new name exists
				if not frappe.db.exists(doctype, new_name):
					frappe.logger().error(f"[RENAME_WITH_NAMING_SERIES] Rename failed - {new_name} does not exist after rename. Not syncing.")
					return  # Don't sync if rename verification failed
				
				# Verify original name no longer exists
				if frappe.db.exists(doctype, document_name):
					frappe.logger().warning(f"[RENAME_WITH_NAMING_SERIES] Original name {document_name} still exists. Waiting and rechecking.")
					time.sleep(1)
					if frappe.db.exists(doctype, document_name):
						frappe.logger().error(f"[RENAME_WITH_NAMING_SERIES] Rename incomplete - original name still exists. Not syncing.")
						return  # Don't sync if rename is incomplete
				
				# Reload doc with new name
				doc = frappe.get_doc(doctype, new_name)
				
				# Set naming series if not already set (this should already be set, but ensure it)
				if doc.get('naming_series') != naming_series:
					try:
						doc.naming_series = naming_series
						doc.save(ignore_permissions=True)
						frappe.db.commit()
					except Exception as naming_series_error:
						# If we can't set naming_series (e.g., "Value cannot be changed for Series"), 
						# that's okay - the rename already happened and the series is correct
						frappe.logger().info(f"[RENAME_WITH_NAMING_SERIES] Could not set naming_series (may already be set): {str(naming_series_error)}")
				
				# Update sync_reference if document is not submitted (for submitted, we already set it above)
				if doc.docstatus != 1 and frappe.db.has_column(doctype, 'sync_reference'):
					try:
						doc.sync_reference = new_name
						doc.sync_type = "Local"
						doc.save(ignore_permissions=True)
						frappe.db.commit()
					except Exception as sync_ref_error:
						frappe.log_error(
							title="Failed to set sync_reference after rename",
							message=f"Could not set sync_reference after renaming {doctype} {new_name}: {str(sync_ref_error)}"
						)
				
				# Only sync if rename was successful
				frappe.logger().info(f"[RENAME_WITH_NAMING_SERIES] Rename successful, queuing sync for {doctype} {new_name}")
				frappe.enqueue(
					_process_sync_on_submit,
					doctype=doctype,
					document_name=new_name,
					queue="short",
					timeout=300,
					is_async=True,
					job_name=f"sync_on_submit_{doctype}_{new_name}"
				)
			except Exception as rename_error:
				error_msg = str(rename_error)
				frappe.log_error(
					title="Failed to rename document with naming series on submit",
					message=f"Could not rename {doctype} {document_name} to {new_name}: {str(rename_error)}"
				)
				frappe.logger().error(f"[RENAME_WITH_NAMING_SERIES] Rename failed for {doctype} {document_name}. Not syncing.")
				return  # Don't sync if rename failed
		else:
			# Name is the same, just set naming series and sync
			if current_naming_series != naming_series:
				doc.naming_series = naming_series
				doc.save(ignore_permissions=True)
			
			# Queue sync with current name (name didn't need to change)
			frappe.logger().info(f"[RENAME_WITH_NAMING_SERIES] Name unchanged, queuing sync for {doctype} {document_name}")
			frappe.enqueue(
				_process_sync_on_submit,
				doctype=doctype,
				document_name=document_name,
				queue="short",
				timeout=300,
				is_async=True,
				job_name=f"sync_on_submit_{doctype}_{document_name}"
			)
	except Exception as e:
		frappe.log_error(
			title="Failed to rename with naming series on submit",
			message=f"Error in _rename_with_naming_series_on_submit for {doctype} {document_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)
		frappe.logger().error(f"[RENAME_WITH_NAMING_SERIES] Error processing rename for {doctype} {document_name}. Not syncing.")
		# Don't sync if there was an error - rename must be successful first


def _process_sync_on_submit(doctype: str, document_name: str):
	"""
	Process sync on submit in background after submit completes
	This function runs ALL checks and operations in background to avoid blocking submit
	"""
	try:
		frappe.logger().info(f"[SYNC_ON_SUBMIT] Processing {doctype} {document_name} in background")
		
		# Get settings in background
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
		
		# Get document to check company and prepare for sync
		try:
			doc = frappe.get_doc(doctype, document_name)
		except frappe.DoesNotExistError:
			frappe.logger().warning(f"[SYNC_ON_SUBMIT] Document {doctype} {document_name} does not exist")
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
					frappe.db.set_value(doctype, document_name, {
						'sync_reference': document_name,
						'sync_type': 'Local'
					}, update_modified=False)
					frappe.db.commit()
					frappe.logger().info(f"[SYNC_ON_SUBMIT] Set sync_reference={document_name} and sync_type=Local on {doctype} {document_name}")
			except Exception as e:
				frappe.log_error(
					title="Failed to set sync fields on document",
					message=f"Could not set sync_reference and sync_type on {doctype} {document_name}: {str(e)}"
				)
		
		# Queue sync job in background
		doc_data = prepare_doc_for_sync(doc)
		queue_sync_job(
			doctype=doctype,
			name=document_name,
			sync_type="Send",
			document_data=doc_data,
			priority=8  # Higher priority for individual document syncs
		)
		frappe.logger().info(f"[SYNC_ON_SUBMIT] Queued sync for {doctype} {document_name}")
		
	except Exception as e:
		frappe.log_error(
			title="Process Sync On Submit Failed",
			message=f"Error in _process_sync_on_submit for {doctype} {document_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)


def _process_sync_on_update(doctype: str, document_name: str):
	"""
	Process sync on update in background after update completes
	This function runs ALL checks and operations in background to avoid blocking update
	"""
	try:
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
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		should_auto_sync = doctype in auto_sync_doctypes
		
		# Only sync if it's in auto-sync doctypes OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			return
		
		# Get document to check company and prepare for sync
		try:
			doc = frappe.get_doc(doctype, document_name)
		except frappe.DoesNotExistError:
			frappe.logger().warning(f"[SYNC_ON_UPDATE] Document {doctype} {document_name} does not exist")
			return
		
		# Check company filter if specified in settings
		company = getattr(settings, 'company', None)
		if company:
			doc_data_check = doc.as_dict()
			if not belongs_to_company(doc_data_check, doctype, company):
				# Document doesn't belong to the selected company, skip syncing
				return
		
		# Queue sync job in background
		doc_data = prepare_doc_for_sync(doc)
		queue_sync_job(
			doctype=doctype,
			name=document_name,
			sync_type="Send",
			document_data=doc_data,
			priority=8  # Higher priority for individual document syncs
		)
		frappe.logger().info(f"[SYNC_ON_UPDATE] Queued sync for {doctype} {document_name}")
		
	except Exception as e:
		frappe.log_error(
			title="Process Sync On Update Failed",
			message=f"Error in _process_sync_on_update for {doctype} {document_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)




def _queue_sync_if_needed(doctype: str, document_name: str):
	"""
	Queue sync if document should be synced
	This runs in background after rename completes
	"""
	try:
		frappe.logger().info(f"[QUEUE_SYNC] Checking if sync needed for {doctype} {document_name}")
		
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			frappe.logger().warning(f"[QUEUE_SYNC] Settings not configured, skipping sync for {doctype} {document_name}")
			return
		
		# Check if sync is enabled
		if not settings.enable_sync:
			frappe.logger().warning(f"[QUEUE_SYNC] Sync is disabled, skipping sync for {doctype} {document_name}")
			return
		
		# Auto-sync doctypes that should always sync (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		
		# Check if this doctype should auto-sync, or if it's enabled for sending
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		frappe.logger().info(f"[QUEUE_SYNC] {doctype} - should_auto_sync: {should_auto_sync}, is_enabled_for_send: {is_enabled_for_send}")
		
		# Only sync if it's an auto-sync doctype OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			frappe.logger().info(f"[QUEUE_SYNC] {doctype} is not auto-sync and not enabled for send, skipping")
			return
		
		# Get document to check company
		try:
			doc = frappe.get_doc(doctype, document_name)
		except frappe.DoesNotExistError:
			frappe.logger().warning(f"[QUEUE_SYNC] Document {doctype} {document_name} does not exist")
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
				frappe.db.set_value(doctype, document_name, {
					'sync_reference': document_name,
					'sync_type': 'Local'
				}, update_modified=False)
				frappe.db.commit()
				frappe.logger().info(f"[QUEUE_SYNC] Set sync_reference={document_name} and sync_type=Local on {doctype} {document_name}")
		except Exception as e:
			frappe.log_error(
				title="Failed to ensure sync fields before queuing sync",
				message=f"Could not ensure sync fields for {doctype} {document_name}: {str(e)}"
			)
			# Continue anyway - try to queue sync
		
		# Queue sync job
		doc_data = prepare_doc_for_sync(doc)
		queue_sync_job(
			doctype=doctype,
			name=document_name,
			sync_type="Send",
			document_data=doc_data,
			priority=8  # Higher priority for individual document syncs
		)
		frappe.logger().info(f"[QUEUE_SYNC] Successfully queued sync for {doctype} {document_name}")
		
	except Exception as e:
		frappe.log_error(
			title="Failed to queue sync after rename",
			message=f"Could not queue sync for {doctype} {document_name}: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)








