# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

# This file imports and re-exports all sync functions for backward compatibility
# Functions have been split into separate modules:
# - utils.py: Utility and helper functions
# - queue.py: Queue and logging functions
# - document_preparation.py: Document preparation functions
# - sync_operations.py: Core sync operations
# - document_events.py: Document event handlers
# - fetch_operations.py: Fetch operations

import frappe
from frappe.utils import cint
from typing import Optional, Dict, Any

# Import from utils
from havano_sync.havano_sync.tasks.utils import (
    serialize_document_data,
    get_sync_settings,
    is_submittable_doctype,
    belongs_to_company,
    get_decrypted_api_secret,
    get_target_url,
    get_syncable_doctypes,
    should_sync_doctype,
    check_internet_connection
)

# Import from queue
from havano_sync.havano_sync.tasks.queue import (
    create_sync_log,
    queue_sync_job,
    process_queued_syncs,
    process_queue_cron_job
)

# Import from document_preparation
from havano_sync.havano_sync.tasks.document_preparation import (
    prepare_doc_for_sync,
    create_minimal_master_document
)

# Import from sync_operations
from havano_sync.havano_sync.tasks.sync_operations import (
    ensure_sync_fields_exist_on_remote,
    handle_link_validation_error,
    sync_linked_documents,
    sync_document_to_remote
)

# Import from document_events
from havano_sync.havano_sync.tasks.document_events import (
    sync_document_on_create,
    sync_document_on_submit,
    sync_document_on_update,
    add_local_suffix_after_insert
)

# Import from fetch_operations
from havano_sync.havano_sync.tasks.fetch_operations import (
    fetch_document_from_remote,
    fetch_all_documents_from_remote
)

# Functions that remain in sync.py

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
				
				# Remove -Local suffix if present (doctype names should never have -Local suffix)
				if doctype_name and doctype_name.endswith("-Local"):
					doctype_name = doctype_name[:-6]  # Remove "-Local" (6 characters)
					frappe.logger().warning(f"Found doctype name with -Local suffix in syncable doctypes: {syncable.doctypes}. Using {doctype_name} instead.")
				
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
						# For Company doctype, only sync the specified company
						filters["name"] = company
				
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
			
			# Remove -Local suffix if present (doctype names should never have -Local suffix)
			if doctype_name and doctype_name.endswith("-Local"):
				doctype_name = doctype_name[:-6]  # Remove "-Local" (6 characters)
				frappe.logger().warning(f"Found doctype name with -Local suffix in syncable doctypes: {syncable.doctypes}. Using {doctype_name} instead.")
			
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
					# For Company doctype, only sync the specified company
					filters["name"] = company
			
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
						"havano_sync.havano_sync.tasks.fetch_operations.fetch_all_documents_from_remote",
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
		
		# Check if doctype is syncable for sending
		# For test sync, we should respect the syncable doctype settings
		from havano_sync.havano_sync.tasks.utils import should_sync_doctype, is_submittable_doctype
		
		# Auto-sync doctypes that should always sync (compulsory doctypes)
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		should_auto_sync = doctype in auto_sync_doctypes
		is_enabled_for_send = should_sync_doctype(doctype, settings, direction="send")
		
		# Only sync if it's an auto-sync doctype OR if it's enabled for sending
		if not should_auto_sync and not is_enabled_for_send:
			frappe.throw(f"Doctype {doctype} is not enabled for sending to remote. Please enable 'Send to Remote' for this doctype in Havano Sync Settings.")
		
		# For submittable doctypes, check if document is submitted
		# Manual sync can work for both submitted and non-submitted documents
		try:
			doc = frappe.get_doc(doctype, name)
			if is_submittable_doctype(doctype):
				if doc.docstatus == 0:
					frappe.logger().info(f"Manual sync for {doctype} {name}: Document is draft (docstatus=0). Will sync as draft.")
				elif doc.docstatus == 1:
					frappe.logger().info(f"Manual sync for {doctype} {name}: Document is submitted (docstatus=1). Will sync as submitted.")
				else:
					frappe.logger().warning(f"Manual sync for {doctype} {name}: Document has docstatus={doc.docstatus}. Will sync with current status.")
		except frappe.DoesNotExistError:
			# For Sales Invoice and Payment Entry with naming series, try to find renamed document
			if doctype in ("Sales Invoice", "Payment Entry"):
				settings = get_sync_settings()
				if settings:
					naming_series_to_check = None
					if doctype == "Payment Entry" and hasattr(settings, 'payment_entry_naming_series') and settings.payment_entry_naming_series:
						naming_series_to_check = settings.payment_entry_naming_series
					elif doctype == "Sales Invoice" and hasattr(settings, 'sales_invoice_naming_series') and settings.sales_invoice_naming_series:
						naming_series_to_check = settings.sales_invoice_naming_series
					
					if naming_series_to_check:
						# Try to find renamed document by pattern matching
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
							""", (prefix + '%', name), as_dict=True)
							
							if recent_docs:
								renamed_name = recent_docs[0].name
								frappe.logger().info(f"Document {doctype} {name} not found, using renamed name {renamed_name}")
								# Try to get the document again with the new name
								try:
									doc = frappe.get_doc(doctype, renamed_name)
									name = renamed_name  # Update name for rest of function
								except frappe.DoesNotExistError:
									frappe.throw(f"Document {doctype} {name} does not exist (also checked renamed version {renamed_name})")
							else:
								# Check if document exists with -Local suffix as fallback
								if not name.endswith("-Local"):
									local_name = f"{name}-Local"
									if frappe.db.exists(doctype, local_name):
										frappe.logger().info(f"Document {doctype} {name} not found, using renamed name {local_name}")
										name = local_name
									else:
										frappe.throw(f"Document {doctype} {name} does not exist (also checked {local_name} and renamed versions)")
								else:
									frappe.throw(f"Document {doctype} {name} does not exist")
						else:
							# Could not parse naming series, fall back to -Local check
							if not name.endswith("-Local"):
								local_name = f"{name}-Local"
								if frappe.db.exists(doctype, local_name):
									frappe.logger().info(f"Document {doctype} {name} not found, using renamed name {local_name}")
									name = local_name
								else:
									frappe.throw(f"Document {doctype} {name} does not exist (also checked {local_name})")
			else:
				# For other doctypes, check if document exists with -Local suffix
				if not name.endswith("-Local"):
					local_name = f"{name}-Local"
					if frappe.db.exists(doctype, local_name):
						frappe.logger().info(f"Document {doctype} {name} not found, using renamed name {local_name}")
						name = local_name
					else:
						frappe.throw(f"Document {doctype} {name} does not exist (also checked {local_name})")
		
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


def fetch_item_prices_and_exchange_rates():
	"""
	Fetch item prices and exchange rates from remote
	"""
	try:
		from havano_sync.havano_sync.tasks.fetch_operations import fetch_all_documents_from_remote
		
		results = {
			"item_prices": None,
			"exchange_rates": None,
			"status": "success"
		}
		
		# Fetch Item Prices
		try:
			item_price_results = fetch_all_documents_from_remote(doctype="Item Price")
			results["item_prices"] = item_price_results
		except Exception as item_price_error:
			frappe.log_error(
				"Failed to fetch Item Prices from remote",
				f"Error fetching Item Prices: {str(item_price_error)}\n{frappe.get_traceback()}"
			)
			results["item_prices"] = {
				"status": "error",
				"message": str(item_price_error)
			}
			results["status"] = "partial"
		
		# Fetch Currency Exchange (try both possible doctype names)
		exchange_doctypes = ["Currency Exchange", "Currency Exchange Rate"]
		exchange_fetched = False
		
		for exchange_doctype in exchange_doctypes:
			if not frappe.db.exists("DocType", exchange_doctype):
				continue
			
			try:
				exchange_results = fetch_all_documents_from_remote(doctype=exchange_doctype)
				results["exchange_rates"] = exchange_results
				exchange_fetched = True
				break
			except Exception as exchange_error:
				# Try next doctype name
				continue
		
		if not exchange_fetched:
			frappe.log_error(
				"Failed to fetch Currency Exchange from remote",
				f"Could not find or fetch Currency Exchange doctype. Tried: {', '.join(exchange_doctypes)}"
			)
			results["exchange_rates"] = {
				"status": "error",
				"message": f"Currency Exchange doctype not found. Tried: {', '.join(exchange_doctypes)}"
			}
			if results["status"] == "success":
				results["status"] = "partial"
		
		return results
		
	except Exception as e:
		frappe.log_error(
			"Fetch Item Prices and Exchange Rates Failed",
			f"Error: {str(e)}\n{frappe.get_traceback()}"
		)
		return {
			"status": "error",
			"message": str(e),
			"item_prices": None,
			"exchange_rates": None
		}


def trigger_fetch_on_login(login_manager=None):
	"""
	Trigger fetch cron job when user logs in
	This runs in background to avoid blocking login
	Fetches multiple doctypes in parallel for faster execution
	
	Args:
		login_manager: LoginManager instance passed by Frappe's on_login hook
	"""
	try:
		from havano_sync.havano_sync.tasks.fetch_operations import fetch_all_documents_from_remote
		
		# Check if sync is enabled before triggering fetch
		settings = get_sync_settings()
		if not settings or not settings.enable_sync:
			return
		
		# Get syncable doctypes with fetch enabled
		syncable_doctypes = get_syncable_doctypes(settings)
		
		# Fetch each doctype in parallel for faster execution
		# This allows multiple doctypes to be fetched simultaneously
		for syncable in syncable_doctypes:
			doctype_name = syncable.doctypes
			
			# Remove -Local suffix if present
			if doctype_name and doctype_name.endswith("-Local"):
				doctype_name = doctype_name[:-6]
			
			# Skip exempted doctypes
			if doctype_name in ("User", "Sales Invoice", "Payment Entry", "Sales Order"):
				continue
			
			# Check if fetch is enabled
			fetch_enabled = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
			if not fetch_enabled:
				continue
			
			# Enqueue each doctype fetch in parallel (short queue for faster processing)
			frappe.enqueue(
				"havano_sync.havano_sync.tasks.fetch_operations.fetch_all_documents_from_remote",
				doctype=doctype_name,
				queue="short",  # Use short queue for faster processing
				timeout=300,  # 5 minutes timeout per doctype
				is_async=True,
				job_name=f"fetch_on_login_{doctype_name}"
			)
	except Exception as e:
		# Silently fail - don't block login if fetch fails
		frappe.log_error(
			"Failed to trigger fetch on login",
			f"Error triggering fetch on login: {str(e)}\n{frappe.get_traceback()}"
		)