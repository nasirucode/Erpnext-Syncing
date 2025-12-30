// Copyright (c) 2025, nasirucode and contributors
// For license information, please see license.txt

frappe.ui.form.on('Payment Entry', {
	refresh: function(frm) {
		// Add Resync Pending button - show for submitted documents
		if (frm.doc.docstatus === 1 && frm.doc.name) {
			frm.add_custom_button(__('Resync Pending'), function() {
				frappe.show_alert({
					message: __('Resyncing Payment Entry...'),
					indicator: 'blue'
				}, 5);
				frappe.call({
					method: 'havano_sync.havano_sync.api.sync.resync_pending_document',
					args: {
						doctype: 'Payment Entry',
						name: frm.doc.name
					},
					// freeze: true,
					// freeze_message: __('Resyncing Payment Entry...'),
					callback: function(r) {
						if (r.message) {
							if (r.message.status === 'success') {
								frappe.show_alert({
									message: __(r.message.message || 'Payment Entry has been queued for resync.'),
									indicator: 'green'
								}, 5);
								
								// Refresh the form to update sync_status
								setTimeout(function() {
									frm.reload_doc();
								}, 2000);
							} else {
								frappe.show_alert({
									message: __(r.message.message || 'Failed to resync Payment Entry.'),
									indicator: 'red'
								}, 5);
							}
						} else {
							frappe.show_alert({
								message: __('Failed to resync Payment Entry.'),
								indicator: 'red'
							}, 5);
						}
					},
					error: function(r) {
						frappe.show_alert({
							message: __('Error resyncing Payment Entry. Please check error logs.'),
							indicator: 'red'
						}, 5);
					}
				});
			});
		}
	}
});

