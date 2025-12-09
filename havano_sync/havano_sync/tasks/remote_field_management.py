# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
import requests
from typing import Any
from havano_sync.havano_sync.utils.sync_api import SyncAPI, DocumentNotFoundError
from havano_sync.havano_sync.tasks.utils import (
    get_sync_settings,
    should_sync_doctype
)
from havano_sync.havano_sync.tasks.document_preparation import prepare_doc_for_sync


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


def ensure_naming_series_on_remote(
	naming_series_name: str,
	api_client: Any,
	target_url: str,
	api_key: str,
	api_secret: str,
	settings: Any = None
) -> bool:
	"""
	Ensure a naming series pattern is available on the remote instance
	In Frappe, naming series are just patterns stored in DocType field options.
	The actual counter is managed automatically by Frappe when documents are created.
	This function just ensures the naming series pattern is in the doctype options.
	
	Note: The actual function that ensures it's in options is ensure_naming_series_in_doctype_options,
	which should be called separately. This function exists for API compatibility.
	
	Args:
		naming_series_name: Name/pattern of the naming series (e.g., "ACC-SINV-STORE1-.YYYY.-")
		api_client: SyncAPI client instance
		target_url: Remote server URL
		api_key: API key for authentication
		api_secret: API secret for authentication
		settings: Havano Sync Settings (optional)
	
	Returns:
		True (naming series patterns don't need to be "created" - they're just strings in options)
	"""
	# In Frappe, naming series are just pattern strings stored in DocType field options.
	# There's no separate "Series" table or DocType to sync.
	# The counter is managed automatically by Frappe when documents are created.
	# We just need to ensure the pattern is in the doctype options, which is handled
	# by ensure_naming_series_in_doctype_options().
	# This function always returns True since we can't really "check" if a pattern exists.
	return True


def ensure_naming_series_in_doctype_options(
	doctype: str,
	naming_series_name: str,
	api_client: Any
) -> bool:
	"""
	Ensure a naming series is in the options for the naming_series field of a doctype
	If it's not in the options, add it
	
	Args:
		doctype: Document type (e.g., "Payment Entry", "Sales Invoice")
		naming_series_name: Name of the naming series to add
		api_client: SyncAPI client instance
	
	Returns:
		True if naming series is in options or was added, False otherwise
	"""
	try:
		# Get the doctype document from remote
		doctype_doc = api_client.get_document("DocType", doctype)
		
		# Find the naming_series field
		fields = doctype_doc.get('fields', [])
		if not isinstance(fields, list):
			return False
		
		naming_series_field = None
		for field in fields:
			if field.get('fieldname') == 'naming_series':
				naming_series_field = field
				break
		
		if not naming_series_field:
			# No naming_series field, nothing to do
			return True
		
		# Get current options
		options = naming_series_field.get('options', '')
		if not options:
			options = ''
		
		# Check if naming series is already in options
		options_list = [opt.strip() for opt in options.split('\n') if opt.strip()]
		if naming_series_name in options_list:
			return True  # Already in options
		
		# Add naming series to options
		options_list.append(naming_series_name)
		naming_series_field['options'] = '\n'.join(options_list)
		
		# Update the doctype on remote
		# Remove metadata fields before saving
		metadata_fields = {'name', 'doctype', 'modified', 'modified_by', 'creation', 'owner', '_user_tags', '_comments', '_assign', '_liked_by', '_seen'}
		doctype_data = {k: v for k, v in doctype_doc.items() if k not in metadata_fields}
		doctype_data['doctype'] = "DocType"
		doctype_data['name'] = doctype
		
		# Update the doctype
		api_client.update_document("DocType", doctype, doctype_data)
		
		return True
		
	except Exception as e:
		frappe.log_error(
			title="Failed to ensure naming series in doctype options",
			message=f"Could not ensure naming series {naming_series_name} is in options for {doctype}: {str(e)}"
		)
		return False

