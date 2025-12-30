# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.utils import now_datetime
from typing import Optional, Dict, Any
from havano_sync.havano_sync.tasks.utils import (
	get_sync_settings,
	check_internet_connection,
	serialize_document_data
)
from havano_sync.havano_sync.tasks.document_preparation import prepare_doc_for_sync
# Note: sync_document_to_remote is imported inside process_queued_syncs to avoid circular import


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
	"""
	Queue a sync job and immediately enqueue it to run in the background
	This ensures save/update/submit operations are not blocked
	
	Note: This function does NOT check for internet connection.
	Jobs are always queued, even without internet, and will be processed
	when internet becomes available via the cron job or retry mechanism.
	"""
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
		# Set the doctype field (the synced doctype, not the queue doctype) using update
		# This is the synced document's doctype (e.g., "Customer", "Sales Invoice")
		queue_doc.update({
			"doctype": doctype
		})
		queue_doc.insert(ignore_permissions=True)
		frappe.db.commit()
		
		# After insert, ensure the doctype field is set correctly using db.set_value
		# This is necessary because the doctype field might conflict with the document's doctype property
		frappe.db.set_value("Havano Sync Queue", queue_doc.name, "doctype", doctype, update_modified=False)
		frappe.db.commit()
		
		# Reload the document to ensure we have the correct field value
		queue_doc.reload()
		
		create_sync_log(
			sync_type=sync_type,
			doctype=doctype,
			document_name=name,
			status="Queued",
			message="Job queued for background processing",
			sync_method="Queue"
		)
		
		# Immediately enqueue the sync job to run in the background
		# This ensures save/update/submit operations are not blocked
		# Store doctype and name in variables to ensure they're passed correctly
		sync_doctype = doctype
		sync_name = name
		
		frappe.enqueue(
			"havano_sync.havano_sync.tasks.queue.process_single_queued_sync",
			queue_doc_name=queue_doc.name,
			doctype=sync_doctype,
			name=sync_name,
			sync_type=sync_type,
			document_data=document_data,
			queue="short",
			timeout=300,
			is_async=True,
			job_name=f"sync_{sync_doctype}_{sync_name}_{sync_type}"
		)
		
		return queue_doc.name
	except Exception as e:
		frappe.log_error(f"Failed to queue sync job: {str(e)}")
	return None


def process_single_queued_sync(
	queue_doc_name: str,
	doctype: str,
	name: str,
	sync_type: str = "Send",
	document_data: Dict[str, Any] = None
):
	"""
	Process a single queued sync job in the background
	This is called via frappe.enqueue to run asynchronously
	"""
	try:
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret or not settings.remote_url:
			frappe.log_error(
				"Sync Failed: Settings not configured",
				f"Failed to sync {doctype} {name}: Settings not properly configured"
			)
			return {"status": "error", "message": "Settings not configured"}
		
		# Check if sync is enabled
		if not settings.enable_sync:
			return {"status": "skipped", "message": "Sync is disabled"}
		
		# Get the queue document
		try:
			queue_doc = frappe.get_doc("Havano Sync Queue", queue_doc_name)
		except frappe.DoesNotExistError:
			frappe.log_error(
				"Sync Failed: Queue document not found",
				f"Queue document {queue_doc_name} not found"
			)
			return {"status": "error", "message": "Queue document not found"}
		
		# Use doctype and name from queue document to ensure correctness
		# This prevents parameter swapping issues
		# Use get() method to access the field value, not the document's doctype property
		queue_doctype = queue_doc.get("doctype")  # Get the field value
		queue_document_name = queue_doc.get("document_name")  # Get the field value
		
		if queue_doctype and queue_document_name:
			doctype = queue_doctype
			name = queue_document_name
		elif not doctype or not name:
			error_msg = f"Invalid parameters: doctype={doctype}, name={name}. Queue doc doctype field={queue_doctype}, document_name={queue_document_name}"
			frappe.log_error(
				"Sync Failed: Invalid parameters",
				error_msg
			)
			# Use db.set_value to update queue status without triggering validation
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
				"status": "Failed",
				"error_message": error_msg
			}, update_modified=False)
			frappe.db.commit()
			return {"status": "error", "message": error_msg}
		
		# Ensure doctype is not "Havano Sync Queue" (this would indicate parameter swap)
		if doctype == "Havano Sync Queue":
			error_msg = f"Parameter error: doctype is 'Havano Sync Queue' but should be the actual document type. name={name}. Queue doc doctype field={queue_doctype}"
			frappe.log_error(
				"Sync Failed: Parameter error",
				error_msg
			)
			# Use db.set_value to update queue status without triggering validation
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
				"status": "Failed",
				"error_message": error_msg
			}, update_modified=False)
			frappe.db.commit()
			return {"status": "error", "message": error_msg}
		
		# Verify document exists before syncing
		# Check this BEFORE updating queue status to avoid issues
		# Documents are no longer renamed, so just check by name
		actual_name = name
		if not frappe.db.exists(doctype, name):
			error_msg = f"Document {doctype} {name} does not exist"
			frappe.log_error(
				"Sync Failed: Document not found",
				error_msg
			)
			# Use db.set_value to update queue status without triggering validation
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
				"status": "Failed",
				"error_message": error_msg
			}, update_modified=False)
			frappe.db.commit()
			return {"status": "error", "message": error_msg}
		
		# Document exists, continue with sync
		actual_name = name
		
		# Update status to Processing using db.set_value to avoid validation issues
		frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
			"status": "Processing",
			"last_attempt_at": now_datetime()
		}, update_modified=False)
		frappe.db.commit()
		
		# Verify document exists and can be loaded
		try:
			# Get document to verify it can be loaded
			doc = frappe.get_doc(doctype, actual_name)
			
			# Get document data if not provided
			if not document_data:
				document_data = prepare_doc_for_sync(doc)
		except frappe.DoesNotExistError as e:
			error_msg = f"Document {doctype} {actual_name} does not exist: {str(e)}"
			frappe.log_error(
				"Sync Failed: Document not found",
				error_msg
			)
			# Use db.set_value to update queue status without triggering validation
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
				"status": "Failed",
				"error_message": error_msg
			}, update_modified=False)
			frappe.db.commit()
			return {"status": "error", "message": error_msg}
		except Exception as e:
			error_msg = f"Could not get document {doctype} {actual_name}: {str(e)}"
			frappe.log_error(
				"Sync Failed: Could not get document",
				error_msg
			)
			# Use db.set_value to update queue status without triggering validation
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
				"status": "Failed",
				"error_message": error_msg
			}, update_modified=False)
			frappe.db.commit()
			return {"status": "error", "message": error_msg}
		
		# Import here to avoid circular dependency
		from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
		
		# For Sales Invoice, Payment Entry, and Quotation, skip naming series check since renaming was removed
		skip_naming_check = doctype in ("Sales Invoice", "Payment Entry", "Quotation")
		
		result = sync_document_to_remote(
			doctype=doctype,
			name=actual_name,
			target_url=settings.remote_url,
			api_key=settings.admin_api_key,
			api_secret=None,  # Will be decrypted in function
			force_create=True,
			sync_method="Queue",
			settings=settings,
			skip_naming_series_check=skip_naming_check
		)
		
		# Update queue status based on result using db.set_value to avoid validation issues
		if result.get("status") == "success":
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
				"status": "Completed"
			}, update_modified=False)
			frappe.db.commit()
		else:
			# Get current retry count
			current_retry_count = frappe.db.get_value("Havano Sync Queue", queue_doc_name, "retry_count") or 0
			max_retries = frappe.db.get_value("Havano Sync Queue", queue_doc_name, "max_retries") or 3
			new_retry_count = current_retry_count + 1
			
			update_data = {
				"retry_count": new_retry_count,
				"error_message": result.get("error", "Sync failed")
			}
			
			if new_retry_count >= max_retries:
				update_data["status"] = "Failed"
				update_data["error_message"] = result.get("error", "Max retries reached")
			else:
				update_data["status"] = "Queued"
				# Calculate next retry time (exponential backoff: 2^retry_count minutes)
				from frappe.utils import add_to_date
				next_retry = add_to_date(now_datetime(), minutes=2 ** new_retry_count)
				update_data["next_retry_at"] = next_retry
			
			frappe.db.set_value("Havano Sync Queue", queue_doc_name, update_data, update_modified=False)
			frappe.db.commit()
		
		return result
		
	except Exception as e:
		error_str = str(e).lower()
		# Check if this is a connection/network error (retryable)
		is_connection_error = any(keyword in error_str for keyword in [
			"connection", "timeout", "network", "unreachable", "dns", 
			"no internet", "internet", "offline", "refused"
		])
		
		frappe.log_error(
			"Process Single Queued Sync Failed",
			frappe.get_traceback()
		)
		
		# Try to update queue status using db.set_value to avoid validation issues
		try:
			# Get current retry count
			current_retry_count = frappe.db.get_value("Havano Sync Queue", queue_doc_name, "retry_count") or 0
			max_retries = frappe.db.get_value("Havano Sync Queue", queue_doc_name, "max_retries") or 3
			new_retry_count = current_retry_count + 1
			
			# For connection errors, retry instead of marking as permanently failed
			if is_connection_error and new_retry_count < max_retries:
				# Mark as Queued for retry with exponential backoff
				from frappe.utils import add_to_date
				next_retry = add_to_date(now_datetime(), minutes=2 ** new_retry_count)
				frappe.db.set_value("Havano Sync Queue", queue_doc_name, {
					"status": "Queued",
					"retry_count": new_retry_count,
					"error_message": f"Connection error (will retry): {str(e)}",
					"next_retry_at": next_retry
				}, update_modified=False)
			else:
				# For other errors or max retries reached, mark as Failed
				status = "Failed" if new_retry_count >= max_retries else "Queued"
				update_data = {
					"status": status,
					"retry_count": new_retry_count,
					"error_message": str(e)
				}
				if status == "Queued":
					# Calculate next retry time (exponential backoff)
					from frappe.utils import add_to_date
					update_data["next_retry_at"] = add_to_date(now_datetime(), minutes=2 ** new_retry_count)
				frappe.db.set_value("Havano Sync Queue", queue_doc_name, update_data, update_modified=False)
			frappe.db.commit()
		except:
			pass
		return {"status": "error", "message": str(e)}


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
				# Update job status to Processing using db.set_value to avoid validation issues
				frappe.db.set_value("Havano Sync Queue", job.name, {
					"status": "Processing",
					"last_attempt_at": now_datetime()
				}, update_modified=False)
				frappe.db.commit()
				
				# Handle case where document might have been renamed with -Local suffix
				actual_name = job.document_name
				if not frappe.db.exists(job.doctype, job.document_name):
					# Check if document exists with -Local suffix
					if not job.document_name.endswith("-Local"):
						local_name = f"{job.document_name}-Local"
						if frappe.db.exists(job.doctype, local_name):
							actual_name = local_name
							frappe.logger().info(f"Document {job.doctype} {job.document_name} not found, using renamed name {actual_name}")
				
				# Parse document data if available
				doc_data = None
				if job.document_data:
					try:
						doc_data = json.loads(job.document_data)
					except:
						pass
				
				# If no document data, get it from the document using actual_name
				if not doc_data:
					try:
						doc = frappe.get_doc(job.doctype, actual_name)
						doc_data = prepare_doc_for_sync(doc)
					except:
						pass
				
				# Import here to avoid circular dependency
				from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
				
				# For Sales Invoice, Payment Entry, and Quotation, skip naming series check since renaming was removed
				skip_naming_check = job.doctype in ("Sales Invoice", "Payment Entry", "Quotation")
				
				# Try to sync using actual_name
				result = sync_document_to_remote(
					job.doctype,
					actual_name,
					settings.remote_url,
					settings.admin_api_key,
					api_secret=None,  # Will be decrypted in function
					force_create=True,
					sync_method="Queue",
					settings=settings,
					skip_naming_series_check=skip_naming_check
				)
				
				if result["status"] == "success":
					# Mark as completed using db.set_value
					frappe.db.set_value("Havano Sync Queue", job.name, {
						"status": "Completed"
					}, update_modified=False)
					frappe.db.commit()
					successful += 1
				else:
					# Get current retry count
					current_retry_count = frappe.db.get_value("Havano Sync Queue", job.name, "retry_count") or 0
					max_retries = frappe.db.get_value("Havano Sync Queue", job.name, "max_retries") or 3
					new_retry_count = current_retry_count + 1
					
					update_data = {
						"retry_count": new_retry_count,
						"error_message": result.get("error", "Sync failed")
					}
					
					if new_retry_count >= max_retries:
						update_data["status"] = "Failed"
						update_data["error_message"] = result.get("error", "Max retries reached")
					else:
						update_data["status"] = "Queued"
						# Calculate next retry time (exponential backoff: 2^retry_count minutes)
						from frappe.utils import add_to_date
						next_retry = add_to_date(now_datetime(), minutes=2 ** new_retry_count)
						update_data["next_retry_at"] = next_retry
					
					frappe.db.set_value("Havano Sync Queue", job.name, update_data, update_modified=False)
					frappe.db.commit()
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

