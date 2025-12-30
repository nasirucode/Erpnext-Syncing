// Copyright (c) 2025, nasirucode and contributors
// For license information, please see license.txt

frappe.listview_settings['Payment Entry'] = {
	add_fields: ["sync_status", "docstatus"],
	get_indicator: function(doc) {
		// You can customize indicators here if needed
	},
	onload: function(listview) {
		// Add Resync All Pending button as custom button in page actions
		// Create a reusable function for the resync logic
		const resyncHandler = function() {
			frappe.confirm(
				__('Are you sure you want to resync all pending Payment Entries? This will queue all pending documents for sync.'),
				function() {
					// Yes
					frappe.show_alert({
						message: __('Resyncing all pending Payment Entries...'),
						indicator: 'blue'
					}, 5);
					
					frappe.call({
						method: 'havano_sync.havano_sync.api.sync.trigger_sync_all',
						args: {
							doctype: 'Payment Entry'
						},
						callback: function(r) {
							if (r.message) {
								if (r.message.status === 'completed' || r.message.status === 'success') {
									const total_synced = r.message.total_synced || 0;
									const total_errors = r.message.total_errors || 0;
									const errors = r.message.results && r.message.results.errors || [];
									
									let message = __('Payment Entries sync completed.');
									if (total_synced > 0) {
										message += ' ' + __('{0} documents synced.', [total_synced]);
									}
									if (total_errors > 0) {
										message += ' ' + __('{0} errors occurred.', [total_errors]);
										
										// Show error details in a dialog
										if (errors.length > 0) {
											let errorDetails = __('Error Details:\n\n');
											errors.slice(0, 10).forEach(function(err) {
												const docName = err.name || err.doctype || 'Unknown';
												const errorMsg = err.message || err.error || 'Unknown error';
												errorDetails += `${docName}: ${errorMsg}\n`;
											});
											if (errors.length > 10) {
												errorDetails += `\n... and ${errors.length - 10} more errors.`;
											}
											
											frappe.msgprint({
												title: __('Sync Errors'),
												message: errorDetails,
												indicator: 'orange'
											});
										}
									}
									frappe.show_alert({
										message: message,
										indicator: total_errors > 0 ? 'orange' : 'green'
									}, 8);
									
									// Refresh the list
									setTimeout(function() {
										listview.refresh();
									}, 2000);
								} else {
									frappe.show_alert({
										message: __(r.message.message || 'Failed to resync Payment Entries.'),
										indicator: 'red'
									}, 5);
								}
							} else {
								frappe.show_alert({
									message: __('Failed to resync Payment Entries.'),
									indicator: 'red'
								}, 5);
							}
						},
						error: function(r) {
							frappe.show_alert({
								message: __('Error resyncing Payment Entries. Please check error logs.'),
								indicator: 'red'
							}, 5);
						}
					});
				},
				function() {
					// No - do nothing
				}
			);
		};
		
		// Add button directly to page actions area
		// Wait for page to be fully loaded
		setTimeout(function() {
			const page_actions = listview.page.page_actions || $('.page-actions');
			if (page_actions && page_actions.length > 0) {
				const btn = $(`<button class="btn btn-primary btn-sm" style="margin-left: 8px;">${__("Resync All Pending")}</button>`);
				btn.on('click', resyncHandler);
				page_actions.append(btn);
			} else {
				// Fallback: try add_action_item if available
				if (listview.page && listview.page.add_action_item) {
					listview.page.add_action_item(__("Resync All Pending"), resyncHandler);
				}
			}
		}, 500);
	}
};

