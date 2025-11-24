# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from havano_sync.havano_sync.tasks.sync import (
	sync_all_pending_documents,
	sync_single_document
)


@frappe.whitelist()
def trigger_sync_all(doctype: str = None):
	"""
	API endpoint to manually trigger sync for all pending documents
	Can optionally filter by doctype
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_sync_all
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_sync_all?doctype=Customer
	"""
	return sync_all_pending_documents(doctype)


@frappe.whitelist()
def trigger_sync_single(doctype: str, name: str):
	"""
	API endpoint to manually trigger sync for a single document
	
	Usage:
		POST /api/method/havano_sync.havano_sync.api.sync.trigger_sync_single
		Body: {"doctype": "Customer", "name": "CUST-001"}
	"""
	return sync_single_document(doctype, name)

