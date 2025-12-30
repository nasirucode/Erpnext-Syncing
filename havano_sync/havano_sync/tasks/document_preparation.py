# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
	from frappe.model.document import Document


def prepare_doc_for_sync(doc) -> Dict[str, Any]:
	"""Prepare document data for syncing (remove internal fields, empty values, and convert date/datetime)"""
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
	
	# Fields to exclude for specific doctypes
	# For Payment Entry, exclude fields that link to Sales Invoice
	excluded_fields_by_doctype = {}
	if doc.doctype == 'Payment Entry':
		excluded_fields_by_doctype['Payment Entry'] = {
			'reference_name',   # Exclude when reference_doctype is Sales Invoice
			'reference_doctype', # Exclude when it's Sales Invoice
			'reference_no',     # Exclude reference number
			'sales_invoice',    # Direct Sales Invoice link if exists
			'against_invoice',  # Against invoice field if exists
		}
	
	# Child tables to exclude for specific doctypes
	# For Payment Entry, exclude Payment References and References child tables
	excluded_child_tables = set()
	if doc.doctype == 'Payment Entry':
		# Exclude Payment References and References child tables (case-insensitive)
		excluded_child_tables.add('payment_references')
		excluded_child_tables.add('payment references')
		excluded_child_tables.add('references')
		excluded_child_tables.add('reference')
	
	# Helper function to check if a value is empty
	def is_empty_value(value):
		"""Check if a value should be considered empty and skipped"""
		if value is None:
			return True
		if isinstance(value, str) and value.strip() == '':
			return True
		if isinstance(value, (list, dict)) and len(value) == 0:
			return True
		return False
	
	# Helper function to convert datetime/date/timedelta to JSON-serializable format
	def convert_datetime_value(value):
		"""Convert datetime/date/timedelta objects to JSON-serializable strings"""
		if isinstance(value, datetime):
			return value.isoformat()
		elif isinstance(value, date):
			return value.isoformat()
		elif isinstance(value, timedelta):
			# Convert timedelta to HH:MM:SS format (Frappe time format)
			total_seconds = int(value.total_seconds())
			hours = total_seconds // 3600
			minutes = (total_seconds % 3600) // 60
			seconds = total_seconds % 60
			return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
		elif isinstance(value, list):
			# Recursively convert datetime objects in lists
			return [convert_datetime_value(item) for item in value]
		elif isinstance(value, dict):
			# Recursively convert datetime objects in dictionaries
			return {k: convert_datetime_value(v) for k, v in value.items()}
		else:
			return value
	
	# Process main document fields - only include fields with values
	cleaned_dict = {}
	for key in doc_dict.keys():
		# Skip internal fields
		if key in exclude_fields or key.startswith('_'):
			continue
		# Remove 'name' and 'naming_series' fields for specific doctypes
		if key in ('name', 'naming_series') and doc.doctype in doctypes_exclude_name:
			continue
		
		# Skip excluded fields for specific doctypes
		if doc.doctype in excluded_fields_by_doctype:
			excluded_fields = excluded_fields_by_doctype[doc.doctype]
			if key in excluded_fields:
				# For Payment Entry, exclude Sales Invoice related fields
				if doc.doctype == 'Payment Entry':
					# Check reference_doctype first to determine if we should exclude reference fields
					reference_doctype_value = doc_dict.get('reference_doctype', '')
					
					if key == 'reference_name':
						# Only exclude reference_name if reference_doctype is Sales Invoice
						if reference_doctype_value == 'Sales Invoice':
							continue  # Skip this field
					elif key == 'reference_doctype':
						# Exclude reference_doctype if it's Sales Invoice
						if doc_dict.get(key) == 'Sales Invoice':
							continue  # Skip this field
					elif key == 'reference_no':
						# Exclude reference_no if reference_doctype is Sales Invoice
						if reference_doctype_value == 'Sales Invoice':
							continue  # Skip this field
					else:
						# Skip other excluded fields (sales_invoice, against_invoice) unconditionally
						continue
		
		value = doc_dict[key]
		
		# Skip empty values (None, empty strings, empty lists, empty dicts)
		if is_empty_value(value):
			continue
		
		# Convert date/datetime/timedelta objects to JSON-serializable format
		# This handles nested structures (lists, dicts) that may contain datetime objects
		cleaned_dict[key] = convert_datetime_value(value)
	
	# Handle child tables - they are already in the dict as lists
	# Just need to clean up internal fields from each child row
	for field in doc.meta.fields:
		if field.fieldtype == "Table" and field.fieldname in doc_dict:
			fieldname = field.fieldname
			
			# Skip excluded child tables (e.g., Payment References for Payment Entry)
			# Check case-insensitively
			if fieldname.lower() in [k.lower() for k in excluded_child_tables]:
				continue
			
			if isinstance(doc_dict[fieldname], list):
				child_table_data = []
				for child_dict in doc_dict[fieldname]:
					if isinstance(child_dict, dict):
						# Remove internal fields from child table and only include fields with values
						cleaned_child = {}
						for child_key, child_value in child_dict.items():
							# Skip internal fields
							if child_key in exclude_fields or child_key.startswith('_'):
								continue
							
							# Skip empty values
							if is_empty_value(child_value):
								continue
							
							# Convert date/datetime/timedelta objects to JSON-serializable format
							# This handles nested structures (lists, dicts) that may contain datetime objects
							cleaned_child[child_key] = convert_datetime_value(child_value)
						
						# Only add child row if it has at least one field (besides doctype)
						if cleaned_child:
							# Add doctype for child table
							cleaned_child['doctype'] = field.options
							child_table_data.append(cleaned_child)
				
				# Only include child table if it has rows
				if child_table_data:
					cleaned_dict[fieldname] = child_table_data
	
	return cleaned_dict


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

