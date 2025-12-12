import frappe


@frappe.whitelist()
def get_payment_terms_template(party_name, party_type, company=None):
	"""
	Override ERPNext's get_payment_terms_template to handle cases where
	customer/supplier doesn't exist (e.g., during sync operations).
	"""
	if party_type not in ("Customer", "Supplier"):
		return
	template = None

	if party_type == "Customer":
		customer = frappe.get_cached_value(
			"Customer", party_name, fieldname=["payment_terms", "customer_group"], as_dict=1
		)
		# Handle case where customer doesn't exist
		if customer:
			template = customer.payment_terms

			if not template and customer.customer_group:
				template = frappe.get_cached_value("Customer Group", customer.customer_group, "payment_terms")
	else:
		supplier = frappe.get_cached_value(
			"Supplier", party_name, fieldname=["payment_terms", "supplier_group"], as_dict=1
		)
		# Handle case where supplier doesn't exist
		if supplier:
			template = supplier.payment_terms
			if not template and supplier.supplier_group:
				template = frappe.get_cached_value("Supplier Group", supplier.supplier_group, "payment_terms")

	if not template and company:
		template = frappe.get_cached_value("Company", company, fieldname="payment_terms")
	return template


def patch_payment_terms_template():
	"""Monkey patch ERPNext's get_payment_terms_template function"""
	try:
		from erpnext.accounts import party as erpnext_party
		if hasattr(erpnext_party, 'get_payment_terms_template'):
			erpnext_party.get_payment_terms_template = get_payment_terms_template
	except (ImportError, AttributeError):
		# ERPNext not installed or function doesn't exist, skip patching
		pass

