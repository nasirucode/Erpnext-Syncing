import frappe


def execute():
	"""Disable all scheduled jobs with methods starting with 'frappe' or 'erpnext'"""
	scheduled_job_type = frappe.qb.DocType("Scheduled Job Type")
	
	# Update jobs where method starts with 'frappe' or 'erpnext'
	frappe.qb.update(scheduled_job_type).set(
		scheduled_job_type.stopped, 1
	).where(
		(scheduled_job_type.method.like("frappe%")) | 
		(scheduled_job_type.method.like("erpnext%"))
	).run()
	
	frappe.db.commit()

