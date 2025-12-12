import frappe


def execute():
	"""
	Create required sync fields (sync_status, sync_reference, sync_type) 
	for all syncable doctypes configured in Havano Sync Settings
	"""
	try:
		# Get sync settings
		settings = frappe.get_single("Havano Sync Settings")
		
		# Get all syncable doctypes
		syncable_doctypes = settings.get("syncable_doctypes", [])
		
		if not syncable_doctypes:
			frappe.logger().info("No syncable doctypes configured. Skipping field creation.")
			return
		
		# Collect unique doctype names
		doctype_names = set()
		for syncable in syncable_doctypes:
			doctype_name = syncable.get("doctypes") or syncable.doctypes
			if doctype_name:
				# Remove -Local suffix if present
				if doctype_name.endswith("-Local"):
					doctype_name = doctype_name[:-6]
				doctype_names.add(doctype_name)
		
		# Also add common auto-sync doctypes
		auto_sync_doctypes = {"Customer", "Sales Invoice", "Payment Entry", "Sales Order"}
		doctype_names.update(auto_sync_doctypes)
		
		created_count = 0
		updated_count = 0
		skipped_count = 0
		
		for doctype_name in sorted(doctype_names):
			try:
				# Check if doctype exists
				if not frappe.db.exists("DocType", doctype_name):
					frappe.logger().info(f"DocType {doctype_name} does not exist. Skipping.")
					skipped_count += 1
					continue
				
				# Get doctype document
				doctype_doc = frappe.get_doc("DocType", doctype_name)
				
				# Check existing fields
				existing_fields = {f.fieldname for f in doctype_doc.fields}
				fields_to_add = []
				fields_to_update = []
				
				# Check and add sync_status field
				if 'sync_status' not in existing_fields:
					# Find the last field index
					max_idx = max([f.idx or 0 for f in doctype_doc.fields] or [0])
					fields_to_add.append({
						"fieldname": "sync_status",
						"fieldtype": "Select",
						"label": "Sync Status",
						"options": "\nPending\nSynced\nFetched",
						"default": "Pending",
						"read_only": 0,
						"no_copy": 1,
						"idx": max_idx + 1
					})
				else:
					# Update existing field if options are different
					for field in doctype_doc.fields:
						if field.fieldname == 'sync_status':
							if field.options != "\nPending\nSynced\nFetched":
								field.options = "\nPending\nSynced\nFetched"
								if not field.default:
									field.default = "Pending"
								fields_to_update.append('sync_status')
				
				# Check and add sync_reference field
				if 'sync_reference' not in existing_fields:
					max_idx = max([f.idx or 0 for f in doctype_doc.fields] or [0])
					fields_to_add.append({
						"fieldname": "sync_reference",
						"fieldtype": "Data",
						"label": "Sync Reference",
						"description": "Reference to the corresponding document in remote/local instance",
						"read_only": 1,
						"no_copy": 1,
						"unique": 1,
						"idx": max_idx + 1
					})
				
				# Check and add sync_type field
				if 'sync_type' not in existing_fields:
					max_idx = max([f.idx or 0 for f in doctype_doc.fields] or [0])
					fields_to_add.append({
						"fieldname": "sync_type",
						"fieldtype": "Select",
						"label": "Sync Type",
						"options": "\nLocal\nRemote",
						"default": "",
						"read_only": 1,
						"no_copy": 1,
						"idx": max_idx + 1
					})
				else:
					# Update existing field if options are different
					for field in doctype_doc.fields:
						if field.fieldname == 'sync_type' and field.options != "\nLocal\nRemote":
							field.options = "\nLocal\nRemote"
							fields_to_update.append('sync_type')
				
				# Add new fields
				if fields_to_add:
					for field_data in fields_to_add:
						doctype_doc.append("fields", field_data)
					doctype_doc.save(ignore_permissions=True)
					frappe.db.commit()
					created_count += len(fields_to_add)
					frappe.logger().info(f"Added {len(fields_to_add)} field(s) to {doctype_name}: {[f['fieldname'] for f in fields_to_add]}")
				
				# Update existing fields
				if fields_to_update:
					doctype_doc.save(ignore_permissions=True)
					frappe.db.commit()
					updated_count += len(fields_to_update)
					frappe.logger().info(f"Updated {len(fields_to_update)} field(s) in {doctype_name}: {fields_to_update}")
				
				# Clear cache
				frappe.clear_cache(doctype=doctype_name)
				
			except Exception as e:
				frappe.log_error(
					title=f"Failed to add sync fields to {doctype_name}",
					message=f"Error adding sync fields to {doctype_name}: {str(e)}\n{frappe.get_traceback()}"
				)
				skipped_count += 1
		
		# Set empty or NULL sync_status values to "Pending" (default value)
		try:
			updated_count = 0
			for doctype_name in sorted(doctype_names):
				try:
					if frappe.db.exists("DocType", doctype_name) and frappe.db.has_column(doctype_name, 'sync_status'):
						# Count documents with empty or NULL sync_status
						count = frappe.db.sql(f"""
							SELECT COUNT(*) FROM `tab{doctype_name}`
							WHERE sync_status IS NULL OR sync_status = ''
						""")
						empty_count = count[0][0] if count and count[0] else 0
						
						if empty_count > 0:
							# Set empty/NULL values to "Pending"
							frappe.db.sql(f"""
								UPDATE `tab{doctype_name}`
								SET sync_status = 'Pending'
								WHERE sync_status IS NULL OR sync_status = ''
							""")
							updated_count += empty_count
				except Exception:
					pass
			
			if updated_count > 0:
				frappe.db.commit()
				frappe.logger().info(f"Set {updated_count} empty sync_status values to 'Pending'")
		except Exception as e:
			frappe.log_error(
				title="Failed to set default sync_status values",
				message=f"Error setting default sync_status values: {str(e)}"
			)
		
		frappe.logger().info(
			f"Sync fields patch completed: {created_count} fields created, "
			f"{updated_count} fields updated, {skipped_count} doctypes skipped"
		)
		
	except Exception as e:
		frappe.log_error(
			title="Failed to execute sync fields patch",
			message=f"Error executing sync fields patch: {str(e)}\n{frappe.get_traceback()}"
		)
		raise

