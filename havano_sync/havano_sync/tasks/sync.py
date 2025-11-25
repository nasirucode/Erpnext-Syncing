# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import now, cint
from havano_sync.havano_sync.utils.sync_api import SyncAPI
from typing import Optional, Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
	from frappe.model.document import Document


def get_sync_settings():
	"""Get Havano Sync Settings"""
	try:
		return frappe.get_single("Havano Sync Settings")
	except:
		frappe.throw("Havano Sync Settings not found. Please configure it first.")


def get_target_url(settings) -> Optional[str]:
	"""Get the target URL based on instance type"""
	if settings.instance_type == "Local":
		return settings.cloud_url
	elif settings.instance_type == "Cloud":
		return settings.local_url
	return None


def get_syncable_doctypes(settings) -> List[Dict[str, Any]]:
	"""Get list of syncable doctypes from settings"""
	return settings.get("syncable_doctypes", [])


def should_sync_doctype(doctype: str, direction: str, settings) -> bool:
	"""
	Check if a doctype should be synced in the given direction
	direction: 'local' or 'cloud'
	"""
	syncable_doctypes = get_syncable_doctypes(settings)
	
	for syncable in syncable_doctypes:
		if syncable.doctypes == doctype:
			if direction == "local" and syncable.local:
				return True
			elif direction == "cloud" and syncable.cloud:
				return True
	return False


def get_sync_direction(doctype: str, settings) -> Optional[str]:
	"""
	Determine sync direction for a doctype
	Returns: 'local', 'cloud', 'both', or None
	"""
	syncable_doctypes = get_syncable_doctypes(settings)
	
	for syncable in syncable_doctypes:
		if syncable.doctypes == doctype:
			local = syncable.local
			cloud = syncable.cloud
			
			if local and cloud:
				return "both"
			elif local:
				return "local"
			elif cloud:
				return "cloud"
	return None


def prepare_doc_for_sync(doc) -> Dict[str, Any]:
	"""Prepare document data for syncing (remove internal fields)"""
	doc_dict = doc.as_dict()
	
	# Remove internal fields that shouldn't be synced
	exclude_fields = [
		'creation', 'modified', 'modified_by', 'owner', 
		'idx', 'docstatus', 'doctype', 'name',
		'_user_tags', '_comments', '_assign', '_liked_by',
		'__islocal', '__unsaved', '__run_link_triggers'
	]
	
	# Also exclude child table internal fields
	for key in list(doc_dict.keys()):
		if key in exclude_fields or key.startswith('_'):
			doc_dict.pop(key, None)
	
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
								cleaned_child[child_key] = child_value
						# Add doctype for child table
						cleaned_child['doctype'] = field.options
						child_table_data.append(cleaned_child)
				doc_dict[fieldname] = child_table_data
	
	return doc_dict


def sync_document_to_remote(
	doctype: str, 
	name: str, 
	target_url: str, 
	api_key: str, 
	api_secret: str,
	force_create: bool = False
) -> Dict[str, Any]:
	"""
	Sync a document to the remote instance
	"""
	try:
		# Get the document
		doc = frappe.get_doc(doctype, name)
		
		# Prepare document data
		doc_data = prepare_doc_for_sync(doc)
		doc_data['doctype'] = doctype
		
		# Initialize API client
		api_client = SyncAPI(target_url, api_key, api_secret)
		
		# Check if document exists on remote
		doc_exists = False
		if not force_create:
			try:
				api_client.get_document(doctype, name)
				doc_exists = True
			except:
				pass
		
		# Create or update document
		if doc_exists and not force_create:
			result = api_client.update_document(doctype, name, doc_data)
			action = "updated"
		else:
			# For new documents, use the same name if possible
			doc_data['name'] = name
			result = api_client.create_document(doctype, doc_data)
			action = "created"
		
		frappe.logger().info(f"Document {doctype} {name} {action} on remote instance")
		
		return {
			"status": "success",
			"action": action,
			"doctype": doctype,
			"name": name,
			"result": result
		}
	
	except Exception as e:
		frappe.log_error(
			title=f"Sync Failed: {doctype} {name}",
			message=frappe.get_traceback()
		)
		return {
			"status": "error",
			"doctype": doctype,
			"name": name,
			"error": str(e)
		}


def sync_document_on_create(doc, method: Optional[str] = None):
	"""
	Sync document when it's created
	This is called via doc_events hook
	"""
	try:
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret:
			return
		
		doctype = doc.doctype
		direction = get_sync_direction(doctype, settings)
		
		if not direction:
			return
		
		# Determine target URL
		target_url = get_target_url(settings)
		if not target_url:
			return
		
		# Sync based on direction
		if direction in ["cloud", "both"] and settings.instance_type == "Local":
			sync_document_to_remote(
				doctype,
				doc.name,
				target_url,
				settings.admin_api_key,
				settings.admin_api_secret,
				force_create=True
			)
		
		if direction in ["local", "both"] and settings.instance_type == "Cloud":
			sync_document_to_remote(
				doctype,
				doc.name,
				target_url,
				settings.admin_api_key,
				settings.admin_api_secret,
				force_create=True
			)
	
	except Exception as e:
		frappe.log_error(
			title=f"Sync on Create Failed: {doc.doctype} {doc.name}",
			message=frappe.get_traceback()
		)


@frappe.whitelist()
def sync_all_pending_documents(doctype: Optional[str] = None):
	"""
	Sync all pending documents (for cron job or manual trigger)
	If doctype is provided, only sync that doctype
	"""
	try:
		settings = get_sync_settings()
		
		# Check if settings are configured
		if not settings.admin_api_key or not settings.admin_api_secret:
			frappe.throw("Havano Sync Settings not properly configured")
		
		target_url = get_target_url(settings)
		if not target_url:
			frappe.throw("Target URL not configured in Havano Sync Settings")
		
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
			
			# Determine sync direction
			direction = get_sync_direction(doctype_name, settings)
			if not direction:
				continue
			
			# Check if we should sync this direction
			should_sync = False
			if settings.instance_type == "Local":
				should_sync = direction in ["cloud", "both"]
			elif settings.instance_type == "Cloud":
				should_sync = direction in ["local", "both"]
			
			if not should_sync:
				continue
			
			# Get all documents of this doctype
			docs = frappe.get_all(
				doctype_name,
				fields=["name"],
				limit=1000  # Limit to prevent timeout
			)
			
			for doc_info in docs:
				result = sync_document_to_remote(
					doctype_name,
					doc_info.name,
					target_url,
					settings.admin_api_key,
					settings.admin_api_secret
				)
				
				if result["status"] == "success":
					results["success"].append(result)
				else:
					results["errors"].append(result)
		
		return {
			"status": "completed",
			"total_synced": len(results["success"]),
			"total_errors": len(results["errors"]),
			"results": results
		}
	
	except Exception as e:
		frappe.log_error(
			title="Sync All Documents Failed",
			message=frappe.get_traceback()
		)
		frappe.throw(f"Sync failed: {str(e)}")


@frappe.whitelist()
def sync_single_document(doctype: str, name: str):
	"""
	Manually sync a single document
	"""
	try:
		settings = get_sync_settings()
		
		if not settings.admin_api_key or not settings.admin_api_secret:
			frappe.throw("Havano Sync Settings not properly configured")
		
		target_url = get_target_url(settings)
		if not target_url:
			frappe.throw("Target URL not configured in Havano Sync Settings")
		
		# Check if doctype is syncable
		direction = get_sync_direction(doctype, settings)
		if not direction:
			frappe.throw(f"Doctype {doctype} is not configured for syncing")
		
		# Check if we should sync this direction
		should_sync = False
		if settings.instance_type == "Local":
			should_sync = direction in ["cloud", "both"]
		elif settings.instance_type == "Cloud":
			should_sync = direction in ["local", "both"]
		
		if not should_sync:
			frappe.throw(f"Doctype {doctype} is not configured to sync from {settings.instance_type} instance")
		
		result = sync_document_to_remote(
			doctype,
			name,
			target_url,
			settings.admin_api_key,
			settings.admin_api_secret
		)
		
		return result
	
	except Exception as e:
		frappe.log_error(
			title=f"Manual Sync Failed: {doctype} {name}",
			message=frappe.get_traceback()
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
			title="Cron Sync Job Failed",
			message=frappe.get_traceback()
		)
