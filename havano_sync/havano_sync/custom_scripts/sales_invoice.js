// Copyright (c) 2025, nasirucode and contributors
// For license information, please see license.txt

frappe.ui.form.on('Sales Invoice', {
	refresh: function(frm) {
        console.log('refresh');
		// Add custom button to fetch Items and Item Prices from remote
		// if (frm.doc.docstatus === 0) { // Only show for draft documents
			// Add custom button as standalone (not in any dropdown/menu)
			frm.add_custom_button(__('Fetch Items & Prices'), function() {
				frappe.call({
					method: 'havano_sync.havano_sync.api.sync.trigger_fetch_items_and_item_prices',
					freeze: true,
					freeze_message: __('Fetching Items and Item Prices from remote...'),
					callback: function(r) {
						if (r.message) {
							if (r.message.status === 'success') {
								frappe.show_alert({
									message: __(r.message.message || 'Items and Item Prices fetch has been queued. They will be fetched in the background.'),
									indicator: 'green'
								}, 5);
								
								// Refresh the form to show any newly fetched items
								setTimeout(function() {
									frm.reload_doc();
								}, 2000);
							} else {
								frappe.show_alert({
									message: __(r.message.message || 'Failed to fetch Items and Item Prices.'),
									indicator: 'red'
								}, 5);
							}
						} else {
							frappe.show_alert({
								message: __('Failed to fetch Items and Item Prices.'),
								indicator: 'red'
							}, 5);
						}
					},
					error: function(r) {
						frappe.show_alert({
							message: __('Error fetching Items and Item Prices. Please check error logs.'),
							indicator: 'red'
						}, 5);
					}
				});
			});
		// }
	}
});

