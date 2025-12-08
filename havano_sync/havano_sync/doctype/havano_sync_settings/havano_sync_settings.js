// Copyright (c) 2025, nasirucode and contributors
// For license information, please see license.txt

frappe.ui.form.on("Havano Sync Settings", {
	refresh(frm) {
		// Add Test Connection button
		frm.add_custom_button(__("Test Connection"), function() {
			// Validate required fields first
			if (!frm.doc.admin_api_key) {
				frappe.show_alert({
					message: __("Please configure Admin API Key before testing connection."),
					indicator: "orange"
				}, 5);
				return;
			}

			if (!frm.doc.remote_url) {
				frappe.show_alert({
					message: __("Please configure Remote Server URL before testing connection."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Password fields cannot be read from form - must be saved first
			// Save form if there are changes, then test connection
			const testConnection = function() {
				frappe.call({
					method: "havano_sync.havano_sync.api.sync.test_connection",
					args: {
						// Don't pass values - API will read from saved document
						// This ensures password field is properly decrypted
					},
					freeze: true,
					freeze_message: __("Testing connection to {0}...", [frm.doc.remote_url]),
					callback: function(r) {
						if (r.message) {
							if (r.message.status === "success") {
								frappe.show_alert({
									message: r.message.message || __("Connection successful!"),
									indicator: "green"
								}, 5);
							} else {
								frappe.show_alert({
									message: r.message.message || __("Connection failed. Please check your settings."),
									indicator: "red"
								}, 5);
							}
						}
					},
					error: function(r) {
						const error_msg = r.message && r.message.message 
							? r.message.message 
							: __("Connection failed. Please check your settings.");
						frappe.show_alert({
							message: error_msg,
							indicator: "red"
						}, 5);
					}
				});
			};

			// Save form first if there are changes (required for password field)
			if (frm.is_dirty()) {
				// Check if password is provided (if it's a new value, it won't be encrypted yet)
				if (frm.doc.admin_api_secret && frm.doc.admin_api_secret.trim() !== '') {
					frm.save().then(function() {
						testConnection();
					}).catch(function(err) {
						// If save fails, still try to test with previously saved values
						testConnection();
					});
				} else {
					frappe.show_alert({
						message: __("Please configure Admin API Secret before testing connection. Save the form first if you've entered a new password."),
						indicator: "orange"
					}, 5);
				}
			} else {
				// No changes, test with saved values
				testConnection();
			}
		}, __("Actions"));

		// Add Get Apps button
		frm.add_custom_button(__("Get Apps"), function() {
			// Validate required fields first
			if (!frm.doc.admin_api_key) {
				frappe.show_alert({
					message: __("Please configure Admin API Key before getting apps."),
					indicator: "orange"
				}, 5);
				return;
			}

			if (!frm.doc.remote_url) {
				frappe.show_alert({
					message: __("Please configure Remote Server URL before getting apps."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Save form first if there are changes (required for password field)
			const getApps = function() {
				frappe.call({
					method: "havano_sync.havano_sync.api.sync.get_remote_apps",
					freeze: true,
					freeze_message: __("Getting apps from remote server..."),
					callback: function(r) {
						if (r.message) {
							if (r.message.status === "success") {
								const apps = r.message.apps || [];
								const count = r.message.count || 0;
								const versions = r.message.versions || {};
								
								if (count > 0) {
									// Create a modal similar to show_about()
									let modal_content = "<div style='padding: 20px;'>";
									
									// Add apps list (similar to show_about format)
									modal_content += "<h4 style='margin-bottom: 15px;'>" + __("Installed Applications") + " (" + count + ")</h4>";
									modal_content += "<div id='remote-app-versions' style='margin-bottom: 20px;'>";
									
									// Display apps in the same format as show_about()
									for (const app_name in versions) {
										const app = versions[app_name];
										let version_text = "";
										
										if (app.branch) {
											version_text = `v${app.branch_version || app.version} (${app.branch})`;
										} else {
											version_text = `v${app.version}`;
										}
										
										modal_content += `<p class='app-version' style='margin: 8px 0; padding: 5px; cursor: pointer;' title='${app_name}: ${app.branch_version || app.version}'>`;
										modal_content += `<b>${app.title || app_name}:</b> ${version_text}`;
										modal_content += "</p>";
									}
									
									modal_content += "</div>";
									modal_content += "</div>";
									
									// Show in a modal dialog
									const dialog = new frappe.ui.Dialog({
										title: __("About Remote Server"),
										fields: [
											{
												fieldtype: "HTML",
												options: modal_content
											}
										],
										size: "large"
									});
									
									// Add click handler to copy app version (like show_about)
									dialog.on_page_show = function() {
										$(dialog.body).find(".app-version").on("click", function() {
											const title = $(this).attr("title");
											if (title) {
												frappe.utils.copy_to_clipboard(title);
												frappe.show_alert({
													message: __("Copied to clipboard"),
													indicator: "green"
												}, 2);
											}
										});
									};
									
									dialog.show();
								} else {
									frappe.show_alert({
										message: r.message.message || __("No apps found on remote server."),
										indicator: "orange"
									}, 5);
								}
							} else {
								frappe.show_alert({
									message: r.message.message || __("Failed to get apps from remote server."),
									indicator: "red"
								}, 5);
							}
						}
					},
					error: function(r) {
						const error_msg = r.message && r.message.message 
							? r.message.message 
							: __("Failed to get apps from remote server.");
						frappe.show_alert({
							message: error_msg,
							indicator: "red"
						}, 5);
					}
				});
			};

			// Save form first if there are changes (required for password field)
			if (frm.is_dirty()) {
				if (frm.doc.admin_api_secret && frm.doc.admin_api_secret.trim() !== '') {
					frm.save().then(function() {
						getApps();
					}).catch(function(err) {
						// If save fails, still try with previously saved values
						getApps();
					});
				} else {
					frappe.show_alert({
						message: __("Please configure Admin API Secret before getting apps. Save the form first if you've entered a new password."),
						indicator: "orange"
					}, 5);
				}
			} else {
				// No changes, use saved values
				getApps();
			}
		}, __("Actions"));

		// Add Test Sync button
		frm.add_custom_button(__("Test Sync"), function() {
			// Validate required fields first
			if (!frm.doc.admin_api_key || !frm.doc.admin_api_secret) {
				frappe.show_alert({
					message: __("Please configure Admin API Key and Admin API Secret before testing sync."),
					indicator: "orange"
				}, 5);
				return;
			}

			if (!frm.doc.remote_url) {
				frappe.show_alert({
					message: __("Please configure Remote Server URL before testing sync."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Check if there are syncable doctypes
			if (!frm.doc.syncable_doctypes || frm.doc.syncable_doctypes.length === 0) {
				frappe.show_alert({
					message: __("Please add at least one syncable doctype before testing sync."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Collect all syncable doctypes with send or fetch enabled
			let test_doctypes = [];
			
			for (let syncable of frm.doc.syncable_doctypes) {
				if (syncable.send || syncable.fetch) {
					test_doctypes.push({
						doctype: syncable.doctypes,
						send: syncable.send || false,
						fetch: syncable.fetch || false
					});
				}
			}

			if (test_doctypes.length === 0) {
				frappe.show_alert({
					message: __("Please enable 'Send to Remote' or 'Fetch from Remote' for at least one doctype before testing sync."),
					indicator: "orange"
				}, 5);
				return;
			}
			
			// Test all enabled doctypes
			test_all_syncable_doctypes(frm, test_doctypes);
		}, __("Actions"));

		// Add Process Queue button
		frm.add_custom_button(__("Process Queue"), function() {
			frappe.call({
				method: "havano_sync.havano_sync.tasks.sync.process_queued_syncs",
				args: {
					limit: 50
				},
				freeze: true,
				freeze_message: __("Processing queued syncs..."),
				callback: function(r) {
					if (r.message) {
						if (r.message.status === "completed") {
							frappe.show_alert({
								message: __("Processed: {0}, Successful: {1}, Failed: {2}", [
									r.message.processed || 0,
									r.message.successful || 0,
									r.message.failed || 0
								]),
								indicator: "green"
							}, 5);
						} else if (r.message.status === "skipped") {
							frappe.show_alert({
								message: r.message.message || __("No internet connection available."),
								indicator: "orange"
							}, 5);
						}
					}
				}
			});
		}, __("Actions"));

		// Add Fetch from Remote button
		frm.add_custom_button(__("Fetch from Remote"), function() {
			// Validate required fields first
			if (!frm.doc.admin_api_key || !frm.doc.admin_api_secret) {
				frappe.show_alert({
					message: __("Please configure Admin API Key and Admin API Secret before fetching from remote."),
					indicator: "orange"
				}, 5);
				return;
			}

			if (!frm.doc.remote_url) {
				frappe.show_alert({
					message: __("Please configure Remote Server URL before fetching from remote."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Check if there are syncable doctypes with fetch enabled
			if (!frm.doc.syncable_doctypes || frm.doc.syncable_doctypes.length === 0) {
				frappe.show_alert({
					message: __("Please add at least one syncable doctype before fetching from remote."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Check if at least one doctype has fetch enabled
			let has_fetch_enabled = false;
			for (let syncable of frm.doc.syncable_doctypes) {
				if (syncable.fetch) {
					has_fetch_enabled = true;
					break;
				}
			}

			if (!has_fetch_enabled) {
				frappe.show_alert({
					message: __("Please enable 'Fetch from Remote' for at least one doctype before fetching."),
					indicator: "orange"
				}, 5);
				return;
			}

			// Save form first if there are changes (required for password field)
			const fetchDocuments = function() {
				frappe.confirm(
					__("This will fetch all documents from remote server for doctypes with 'Fetch from Remote' enabled. Documents that already exist locally will be skipped. Continue?"),
					function() {
						// Yes
						frappe.call({
							method: "havano_sync.havano_sync.api.sync.trigger_fetch_all",
							freeze: true,
							freeze_message: __("Fetching documents from remote server..."),
							callback: function(r) {
								if (r.message) {
									if (r.message.status === "completed" || r.message.status === "success") {
										const results = r.message.results || {};
										const success_count = results.success ? results.success.length : 0;
										const skipped_count = results.skipped ? results.skipped.length : 0;
										const error_count = results.errors ? results.errors.length : 0;
										
										let message = __("Fetch completed!");
										if (success_count > 0) {
											message += ` ${success_count} document(s) fetched.`;
										}
										if (skipped_count > 0) {
											message += ` ${skipped_count} document(s) skipped (already exist locally).`;
										}
										if (error_count > 0) {
											message += ` ${error_count} error(s) occurred.`;
										}
										
										frappe.show_alert({
											message: message,
											indicator: error_count > 0 ? "orange" : "green"
										}, 8);
									} else {
										frappe.show_alert({
											message: r.message.message || __("Failed to fetch documents from remote server."),
											indicator: "red"
										}, 5);
									}
								}
							},
							error: function(r) {
								const error_msg = r.message && r.message.message 
									? r.message.message 
									: __("Failed to fetch documents from remote server.");
								frappe.show_alert({
									message: error_msg,
									indicator: "red"
								}, 5);
							}
						});
					}
				);
			};

			if (frm.is_dirty()) {
				frm.save().then(function() {
					fetchDocuments();
				}).catch(function(err) {
					fetchDocuments();
				});
			} else {
				fetchDocuments();
			}
		}, __("Actions"));

		// Add Fix Renamed DocTypes button
		frm.add_custom_button(__("Fix Renamed DocTypes"), function() {
			frappe.confirm(
				__("This will fix all DocType definitions that were incorrectly renamed with '-Local' suffix. This will rename them back to their original names (e.g., 'Customer-Local' → 'Customer'). Continue?"),
				function() {
					// Yes
					frappe.call({
						method: "havano_sync.havano_sync.api.sync.fix_renamed_doctypes_job",
						freeze: true,
						freeze_message: __("Fixing renamed DocTypes..."),
						callback: function(r) {
							if (r.message) {
								const fixed_count = r.message.fixed_count || 0;
								const error_count = r.message.error_count || 0;
								const skipped_count = r.message.skipped_count || 0;
								const total_found = r.message.total_found || 0;
								
								// Build detailed message
								let details_html = "<div style='max-height: 400px; overflow-y: auto;'>";
								
								if (fixed_count > 0 && r.message.fixed_doctypes) {
									details_html += "<h4 style='color: green;'>Fixed DocTypes:</h4><ul>";
									r.message.fixed_doctypes.forEach(function(item) {
										details_html += `<li>${item.original} → ${item.fixed}</li>`;
									});
									details_html += "</ul>";
								}
								
								if (error_count > 0 && r.message.errors) {
									details_html += `<h4 style='color: red;'>Errors (${error_count}):</h4><ul style='max-height: 200px; overflow-y: auto;'>`;
									// Show first 20 errors
									const errors_to_show = r.message.errors.slice(0, 20);
									errors_to_show.forEach(function(error) {
										const error_text = typeof error === 'string' ? error : 
											(error.doctype ? `${error.doctype}: ${error.error || error}` : JSON.stringify(error));
										details_html += `<li style='color: red; font-size: 11px;'>${error_text}</li>`;
									});
									details_html += "</ul>";
									if (error_count > 20) {
										details_html += `<p style='color: orange;'>... and ${error_count - 20} more errors. Check Error Log for full details.</p>`;
									}
								}
								
								if (skipped_count > 0 && r.message.skipped) {
									details_html += `<h4 style='color: orange;'>Skipped (${skipped_count}):</h4><ul>`;
									r.message.skipped.slice(0, 10).forEach(function(item) {
										details_html += `<li>${item.doctype}: ${item.reason}</li>`;
									});
									details_html += "</ul>";
									if (skipped_count > 10) {
										details_html += `<p>... and ${skipped_count - 10} more skipped.</p>`;
									}
								}
								
								details_html += "</div>";
								
								// Show summary
								let summary = r.message.message || __("Fix completed!");
								if (total_found > 0) {
									summary += `\n\nFound: ${total_found}, Fixed: ${fixed_count}, Errors: ${error_count}, Skipped: ${skipped_count}`;
								}
								
								// Show dialog with details
								const dialog = new frappe.ui.Dialog({
									title: __("Fix Renamed DocTypes - Results"),
									fields: [
										{
											fieldtype: "HTML",
											options: details_html
										}
									],
									size: "large"
								});
								
								dialog.show();
								
								// Also show alert
								frappe.show_alert({
									message: summary,
									indicator: error_count > 0 ? "orange" : (fixed_count > 0 ? "green" : "blue")
								}, 8);
								
								// Log to console for debugging
								console.log("Fix Renamed DocTypes - Full Response:", r.message);
								
								// Reload the form to reflect any changes
								if (fixed_count > 0) {
									frm.reload_doc();
								}
							}
						},
						error: function(r) {
							const error_msg = r.message && r.message.message 
								? r.message.message 
								: __("Failed to fix renamed DocTypes. Check Error Log for details.");
							frappe.show_alert({
								message: error_msg,
								indicator: "red"
							}, 5);
						}
					});
				},
				function() {
					// No - do nothing
				}
			);
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
					} else if (r.message && r.message.status === "queued") {
						frappe.show_alert({
							message: __("No internet connection. Document queued for sync."),
							indicator: "orange"
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

function test_fetch_document(frm, doctype) {
	// Get a sample document from remote to test fetch
	frappe.call({
		method: "havano_sync.havano_sync.api.sync.trigger_fetch_all",
		args: {
			doctype: doctype
		},
		freeze: true,
		freeze_message: __("Testing fetch from remote..."),
		callback: function(r) {
			if (r.message) {
				if (r.message.status === "completed" || r.message.status === "success") {
					const results = r.message.results || {};
					const success_count = results.success ? results.success.length : 0;
					const skipped_count = results.skipped ? results.skipped.length : 0;
					const error_count = results.errors ? results.errors.length : 0;
					
					let message = __("Fetch test completed!");
					if (success_count > 0) {
						message += ` ${success_count} document(s) fetched.`;
					}
					if (skipped_count > 0) {
						message += ` ${skipped_count} document(s) skipped (already exist locally).`;
					}
					if (error_count > 0) {
						message += ` ${error_count} error(s) occurred.`;
					}
					
					frappe.show_alert({
						message: message,
						indicator: error_count > 0 ? "orange" : "green"
					}, 5);
				} else {
					frappe.show_alert({
						message: r.message.message || __("Fetch test failed. Check Error Log for details."),
						indicator: "red"
					}, 5);
				}
			}
		},
		error: function(r) {
			frappe.show_alert({
				message: __("Fetch test failed. Check Error Log for details."),
				indicator: "red"
			}, 5);
		}
	});
}

function test_all_syncable_doctypes(frm, test_doctypes) {
	// Test all syncable doctypes sequentially
	let results = {
		send: { success: [], errors: [] },
		fetch: { success: [], errors: [] }
	};
	let current_index = 0;
	
	const processNextDoctype = function() {
		if (current_index >= test_doctypes.length) {
			// All doctypes processed, show summary
			let message = __("Test Sync Completed!\n\n");
			
			// Send results
			if (results.send.success.length > 0 || results.send.errors.length > 0) {
				message += __("Send Results:\n");
				message += __("  - Success: {0} doctype(s)\n", [results.send.success.length]);
				message += __("  - Errors: {0} doctype(s)\n\n", [results.send.errors.length]);
			}
			
			// Fetch results
			if (results.fetch.success.length > 0 || results.fetch.errors.length > 0) {
				message += __("Fetch Results:\n");
				message += __("  - Success: {0} doctype(s)\n", [results.fetch.success.length]);
				message += __("  - Errors: {0} doctype(s)\n", [results.fetch.errors.length]);
			}
			
			const has_errors = results.send.errors.length > 0 || results.fetch.errors.length > 0;
			frappe.show_alert({
				message: message,
				indicator: has_errors ? "orange" : "green"
			}, 8);
			return;
		}
		
		const test_doctype = test_doctypes[current_index];
		current_index++;
		
		// Process this doctype
		if (test_doctype.send && test_doctype.fetch) {
			// Both enabled - test send first, then fetch
			test_sync_and_fetch_for_doctype(frm, test_doctype.doctype, results, processNextDoctype);
		} else if (test_doctype.send) {
			// Only send enabled
			test_send_for_doctype(frm, test_doctype.doctype, results, processNextDoctype);
		} else if (test_doctype.fetch) {
			// Only fetch enabled
			test_fetch_for_doctype(frm, test_doctype.doctype, results, processNextDoctype);
		} else {
			// Skip and process next
			processNextDoctype();
		}
	};
	
	// Start processing
	frappe.show_alert({
		message: __("Testing sync for {0} doctype(s)...", [test_doctypes.length]),
		indicator: "blue"
	}, 3);
	
	processNextDoctype();
}

function test_send_for_doctype(frm, doctype, results, callback) {
	// Get a sample document from that doctype
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: doctype,
			limit_page_length: 1
		},
		callback: function(r) {
			if (r.message && r.message.length > 0) {
				const doc_name = r.message[0].name;
				frappe.call({
					method: "havano_sync.havano_sync.api.sync.trigger_sync_single",
					args: {
						doctype: doctype,
						name: doc_name
					},
					freeze: false,
					callback: function(send_r) {
						if (send_r.message && send_r.message.status === "success") {
							results.send.success.push(doctype);
						} else {
							results.send.errors.push(doctype);
						}
						callback();
					},
					error: function() {
						results.send.errors.push(doctype);
						callback();
					}
				});
			} else {
				// No documents found, skip
				callback();
			}
		},
		error: function() {
			results.send.errors.push(doctype);
			callback();
		}
	});
}

function test_fetch_for_doctype(frm, doctype, results, callback) {
	frappe.call({
		method: "havano_sync.havano_sync.api.sync.trigger_fetch_all",
		args: {
			doctype: doctype
		},
		freeze: false,
		callback: function(fetch_r) {
			if (fetch_r.message && (fetch_r.message.status === "completed" || fetch_r.message.status === "success")) {
				results.fetch.success.push(doctype);
			} else {
				results.fetch.errors.push(doctype);
			}
			callback();
		},
		error: function() {
			results.fetch.errors.push(doctype);
			callback();
		}
	});
}

function test_sync_and_fetch_for_doctype(frm, doctype, results, callback) {
	// First test send
	test_send_for_doctype(frm, doctype, results, function() {
		// Then test fetch
		test_fetch_for_doctype(frm, doctype, results, callback);
	});
}

function test_sync_and_fetch(frm, doctype) {
	// First test sending to remote
	// Get a sample document from that doctype
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: doctype,
			limit_page_length: 1
		},
		callback: function(r) {
			if (r.message && r.message.length > 0) {
				const doc_name = r.message[0].name;
				// Test sending first
				frappe.call({
					method: "havano_sync.havano_sync.api.sync.trigger_sync_single",
					args: {
						doctype: doctype,
						name: doc_name
					},
					freeze: true,
					freeze_message: __("Testing send to remote..."),
					callback: function(send_r) {
						let send_message = "";
						if (send_r.message && send_r.message.status === "success") {
							send_message = __("Send test successful! Document {0} was {1} on remote instance.", [doc_name, send_r.message.action || "synced"]);
						} else if (send_r.message && send_r.message.status === "queued") {
							send_message = __("No internet connection. Document queued for sync.");
						} else {
							send_message = __("Send test failed. Check Error Log for details.");
						}
						
						// Then test fetching
						frappe.call({
							method: "havano_sync.havano_sync.api.sync.trigger_fetch_all",
							args: {
								doctype: doctype
							},
							freeze: true,
							freeze_message: __("Testing fetch from remote..."),
							callback: function(fetch_r) {
								let fetch_message = "";
								if (fetch_r.message) {
									if (fetch_r.message.status === "completed" || fetch_r.message.status === "success") {
										const results = fetch_r.message.results || {};
										const success_count = results.success ? results.success.length : 0;
										const skipped_count = results.skipped ? results.skipped.length : 0;
										const error_count = results.errors ? results.errors.length : 0;
										
										fetch_message = __("Fetch test completed!");
										if (success_count > 0) {
											fetch_message += ` ${success_count} document(s) fetched.`;
										}
										if (skipped_count > 0) {
											fetch_message += ` ${skipped_count} document(s) skipped (already exist locally).`;
										}
										if (error_count > 0) {
											fetch_message += ` ${error_count} error(s) occurred.`;
										}
									} else {
										fetch_message = fetch_r.message.message || __("Fetch test failed. Check Error Log for details.");
									}
								}
								
								// Show combined results
								const combined_message = send_message + "\n" + fetch_message;
								const has_errors = (send_r.message && send_r.message.status !== "success" && send_r.message.status !== "queued") ||
												  (fetch_r.message && fetch_r.message.status !== "completed" && fetch_r.message.status !== "success");
								
								frappe.show_alert({
									message: combined_message,
									indicator: has_errors ? "orange" : "green"
								}, 8);
							},
							error: function(fetch_r) {
								frappe.show_alert({
									message: send_message + "\n" + __("Fetch test failed. Check Error Log for details."),
									indicator: "orange"
								}, 8);
							}
						});
					},
					error: function(send_r) {
						frappe.show_alert({
							message: __("Send test failed. Check Error Log for details."),
							indicator: "red"
						}, 5);
					}
				});
			} else {
				frappe.show_alert({
					message: __("No documents found in {0}. Please create at least one document to test sync.", [doctype]),
					indicator: "orange"
				}, 5);
			}
		}
	});
}
