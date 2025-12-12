app_name = "havano_sync"
app_title = "Havano Sync"
app_publisher = "nasirucode"
app_description = "Havano Sync"
app_email = "akingbolahan12@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "havano_sync",
# 		"logo": "/assets/havano_sync/logo.png",
# 		"title": "Havano Sync",
# 		"route": "/havano_sync",
# 		"has_permission": "havano_sync.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/havano_sync/css/havano_sync.css"
# app_include_js = "/assets/havano_sync/js/havano_sync.js"

# include js, css files in header of web template
# web_include_css = "/assets/havano_sync/css/havano_sync.css"
# web_include_js = "/assets/havano_sync/js/havano_sync.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "havano_sync/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "havano_sync/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "havano_sync.utils.jinja_methods",
# 	"filters": "havano_sync.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "havano_sync.install.before_install"
after_install = "havano_sync.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "havano_sync.uninstall.before_uninstall"
# after_uninstall = "havano_sync.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "havano_sync.utils.before_app_install"
# after_app_install = "havano_sync.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "havano_sync.utils.before_app_uninstall"
# after_app_uninstall = "havano_sync.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "havano_sync.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"*": {
		"on_submit": "havano_sync.havano_sync.tasks.sync.sync_document_on_submit",
		"on_update": "havano_sync.havano_sync.tasks.sync.sync_document_on_update"
	},
	"DocType": {
		# Explicitly exclude DocType from all hooks - DocType definitions should never be renamed or synced
	},
	"Customer": {
		"after_insert": "havano_sync.havano_sync.tasks.sync.sync_document_on_create"
	},
	"Sales Invoice": {
		"after_insert": "havano_sync.havano_sync.tasks.sync.sync_document_on_create"
	},
	"Payment Entry": {
		"after_insert": "havano_sync.havano_sync.tasks.sync.sync_document_on_create"
	},
	"Sales Order": {
		"after_insert": "havano_sync.havano_sync.tasks.sync.sync_document_on_create"
	}
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"0 */2 * * *": [
			"havano_sync.havano_sync.tasks.sync.sync_cron_job"
		],
		"*/15 * * * *": [
			"havano_sync.havano_sync.tasks.sync.process_queue_cron_job"
		],
		"*/5 * * * *": [
			"havano_sync.havano_sync.tasks.sync_operations.rename_remote_sales_invoices_by_sync_reference",
			"havano_sync.havano_sync.tasks.sync.check_internet_and_sync_cron_job"
		]
	}
}

# Testing
# -------

# before_tests = "havano_sync.install.before_tests"

# Overriding Methods
# ------------------------------

# override_whitelisted_methods = {
# 	"erpnext.accounts.party.get_payment_terms_template": "havano_sync.havano_sync.utils.party.get_payment_terms_template"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "havano_sync.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["havano_sync.utils.before_request"]
# after_request = ["havano_sync.utils.after_request"]

# Job Events
# ----------
# before_job = ["havano_sync.utils.before_job"]
# after_job = ["havano_sync.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"havano_sync.auth.validate"
# ]

# On Login Hook
# -------------
# Function to call when user logs in

on_session_creation = "havano_sync.havano_sync.tasks.sync.trigger_fetch_on_login"

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

default_log_clearing_doctypes = {
	"Havano Sync Log": 30  # days to retain logs
}

