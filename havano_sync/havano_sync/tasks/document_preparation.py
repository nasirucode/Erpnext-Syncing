# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
	from frappe.model.document import Document


def prepare_doc_for_sync(doc) -> Dict[str, Any]:
	"""Prepare document data for syncing (remove internal fields and convert date/datetime)"""
	doc_dict = doc.as_dict()
	
	# Remove internal fields that shouldn't be synced
	exclude_fields = [
		'creation', 'modified', 'modified_by', 'owner', 
		'idx', 'doctype',
		'_user_tags', '_comments', '_assign', '_liked_by',
		'__islocal', '__unsaved', '__run_link_triggers'
	]
	# Note: 
	# - docstatus is NOT excluded - we need it for submittable doctypes
	# - name is NOT excluded by default - we need to sync the document name (with -Local suffix) to remote
	# - However, name and naming_series IS excluded for Sales Invoice, Payment Entry, and Quotation
	#   (remote will generate the name using its own naming series)
	
	# Doctypes that should not include 'name' and 'naming_series' fields in sync data
	doctypes_exclude_name = {'Sales Invoice', 'Payment Entry', 'Quotation'}
	
	# Also exclude child table internal fields
	for key in list(doc_dict.keys()):
		if key in exclude_fields or key.startswith('_'):
			doc_dict.pop(key, None)
		# Remove 'name' and 'naming_series' fields for specific doctypes
		elif key in ('name', 'naming_series') and doc.doctype in doctypes_exclude_name:
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

