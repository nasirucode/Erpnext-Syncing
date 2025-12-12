# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import json
import os


def after_install():
	"""
	Enable developer mode after app installation
	This makes it easier to add fields to doctypes
	"""
	try:
		# Get the site name
		site_name = frappe.local.site
		
		# Get the site config path
		site_path = frappe.get_site_path()
		site_config_path = os.path.join(site_path, "site_config.json")
		
		# Read existing site config
		site_config = {}
		if os.path.exists(site_config_path):
			with open(site_config_path, 'r') as f:
				site_config = json.load(f)
		
		# Enable developer mode
		site_config["developer_mode"] = 1
		
		# Write back to site_config.json
		with open(site_config_path, 'w') as f:
			json.dump(site_config, f, indent=2)
		
		frappe.logger().info(f"Developer mode enabled for site {site_name}")
		
		# Clear cache to apply changes
		frappe.clear_cache()
		
	except Exception as e:
		frappe.log_error(
			title="Failed to enable developer mode",
			message=f"Error enabling developer mode: {str(e)}\n{frappe.get_traceback()}"
		)

