// Copyright (c) 2025, nasirucode and contributors
// For license information, please see license.txt

frappe.ui.form.on("Havano Sync Settings", {
	refresh(frm) {
		// Add Test Connection button
		frm.add_custom_button(__("Test Connection"), function() {
			// Validate required fields first
			if (!frm.doc.admin_api_key || !frm.doc.admin_api_secret) {
				frappe.msgprint({
					title: __("Configuration Required"),
					message: __("Please configure Admin API Key and Admin API Secret before testing connection."),
					indicator: "orange"
				});
				return;
			}

			const target_url = frm.doc.instance_type === "Local" ? frm.doc.cloud_url : frm.doc.local_url;
			if (!target_url) {
				frappe.msgprint({
					title: __("Configuration Required"),
					message: __("Please configure the target URL ({0}) before testing connection.", [
						frm.doc.instance_type === "Local" ? "Cloud URL" : "Local URL"
					]),
					indicator: "orange"
				});
				return;
			}

			frm.call({
				method: "test_connection",
				args: {},
				freeze: true,
				freeze_message: __("Testing connection to {0}...", [target_url]),
				callback: function(r) {
					if (r.message) {
						if (r.message.status === "success") {
							frappe.show_alert({
								message: r.message.message || __("Connection successful!"),
								indicator: "green"
							}, 5);
						} else {
							frappe.msgprint({
								title: __("Connection Test Failed"),
								message: r.message.message || __("Connection failed. Please check your settings."),
								indicator: "red"
							});
						}
					}
				},
				error: function(r) {
					const error_msg = r.message && r.message.message 
						? r.message.message 
						: __("Connection failed. Please check your settings.");
					frappe.msgprint({
						title: __("Connection Test Failed"),
						message: error_msg,
						indicator: "red"
					});
				}
			});
		}, __("Actions"));

		// Add Test Sync button
		frm.add_custom_button(__("Test Sync"), function() {
			// Validate required fields first
			if (!frm.doc.admin_api_key || !frm.doc.admin_api_secret) {
				frappe.msgprint({
					title: __("Configuration Required"),
					message: __("Please configure Admin API Key and Admin API Secret before testing sync."),
					indicator: "orange"
				});
				return;
			}

			const target_url = frm.doc.instance_type === "Local" ? frm.doc.cloud_url : frm.doc.local_url;
			if (!target_url) {
				frappe.msgprint({
					title: __("Configuration Required"),
					message: __("Please configure the target URL ({0}) before testing sync.", [
						frm.doc.instance_type === "Local" ? "Cloud URL" : "Local URL"
					]),
					indicator: "orange"
				});
				return;
			}

			// Check if there are syncable doctypes
			if (!frm.doc.syncable_doctypes || frm.doc.syncable_doctypes.length === 0) {
				frappe.msgprint({
					title: __("No Syncable Doctypes"),
					message: __("Please add at least one syncable doctype before testing sync."),
					indicator: "orange"
				});
				return;
			}

			// Find first syncable doctype that should sync in the current direction
			let test_doctype = null;
			for (let syncable of frm.doc.syncable_doctypes) {
				if (frm.doc.instance_type === "Local" && (syncable.cloud || (syncable.local && syncable.cloud))) {
					test_doctype = syncable.doctypes;
					break;
				} else if (frm.doc.instance_type === "Cloud" && (syncable.local || (syncable.local && syncable.cloud))) {
					test_doctype = syncable.doctypes;
					break;
				}
			}

			if (!test_doctype) {
				frappe.msgprint({
					title: __("No Syncable Doctypes"),
					message: __("No doctypes configured to sync from {0} instance. Please configure at least one doctype to sync.", [frm.doc.instance_type]),
					indicator: "orange"
				});
				return;
			}

			// Get a sample document from that doctype
			frappe.call({
				method: "frappe.client.get_list",
				args: {
					doctype: test_doctype,
					limit_page_length: 1
				},
				callback: function(r) {
					if (r.message && r.message.length > 0) {
						const doc_name = r.message[0].name;
						test_sync_document(frm, test_doctype, doc_name);
					} else {
						frappe.msgprint({
							title: __("No Documents Found"),
							message: __("No documents found in {0}. Please create at least one document to test sync.", [test_doctype]),
							indicator: "orange"
						});
					}
				}
			});
		}, __("Actions"));
	}
});

function test_sync_document(frm, doctype, name) {
	frappe.confirm(
		__("This will test syncing {0} document '{1}' to the remote instance. Continue?", [doctype, name]),
		function() {
			// Yes
			frappe.call({
				method: "havano_sync.havano_sync.api.sync.trigger_sync_single",
				args: {
					doctype: doctype,
					name: name
				},
				freeze: true,
				freeze_message: __("Testing sync..."),
				callback: function(r) {
					if (r.message && r.message.status === "success") {
						frappe.show_alert({
							message: __("Sync test successful! Document {0} was {1} on remote instance.", [name, r.message.action]),
							indicator: "green"
						}, 5);
					} else {
						frappe.show_alert({
							message: __("Sync test failed. Check Error Log for details."),
							indicator: "red"
						}, 5);
					}
				},
				error: function(r) {
					frappe.show_alert({
						message: __("Sync test failed. Check Error Log for details."),
						indicator: "red"
					}, 5);
				}
			});
		},
		function() {
			// No
		}
	);
}
