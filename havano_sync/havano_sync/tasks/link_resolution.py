# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import requests
from typing import Optional, Dict, Any, TYPE_CHECKING
from frappe.utils import cint
from havano_sync.havano_sync.utils.sync_api import SyncAPI, DocumentNotFoundError
from havano_sync.havano_sync.tasks.utils import get_sync_settings
from havano_sync.havano_sync.tasks.document_preparation import create_minimal_master_document

if TYPE_CHECKING:
    from frappe.model.document import Document


def handle_link_validation_error(
    error: Exception,
    doctype: str, 
    name: str, 
    api_client: Any,
    settings: Any,
    target_url: str, 
    api_key: str, 
    api_secret: str,
    direction: str = "send"
) -> bool:
    """
    Handle LinkValidationError by creating missing linked documents
    
    Args:
        error: The LinkValidationError exception
        doctype: The doctype that failed validation
        name: The document name that failed validation
        api_client: SyncAPI client instance
        settings: Havano Sync Settings
        target_url: Remote server URL
        api_key: API key for authentication
        api_secret: API secret for authentication
        direction: "send" (to remote) or "fetch" (from remote)
    
    Returns:
        True if missing documents were created and operation should be retried, False otherwise
    """
    # Import here to avoid circular dependency
    from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
    
    error_msg = str(error)
    # Clean up error message: remove newlines, extra quotes, and whitespace
    error_msg = error_msg.replace("\n", " ").replace('\\n', " ").replace('"]', "").replace('"', "").strip()
    
    # Parse error message to extract missing documents
    # Format: "Could not find Default Cash Account: Cash - IC B, Default Receivable Account: Debtors - IC B, ..."
    # Or: "Could not find Company: Havanno, Parent Account: Bank Accounts - H"
    missing_docs = []
    
    if "Could not find" in error_msg:
        # Extract the part after "Could not find"
        missing_part = error_msg.split("Could not find", 1)[1].strip()
        
        # Split by comma to get individual missing items
        items = [item.strip() for item in missing_part.split(",")]
        
        # Get document meta to find field definitions
        try:
            meta = frappe.get_meta(doctype)
        except Exception:
            frappe.log_error(
                title="Failed to get meta for LinkValidationError handling",
                message=f"Could not get meta for {doctype}: {str(error)}"
            )
            return False
        
        # Parse each item: "Field Label: Document Name"
        for item in items:
            if ":" in item:
                parts = item.split(":", 1)
                if len(parts) == 2:
                    field_label = parts[0].strip()
                    doc_name = parts[1].strip()
                    
                    # Clean up doc_name: remove newlines, quotes, and extra whitespace
                    doc_name = doc_name.replace("\n", "").replace('"', "").replace("'", "").strip()
                    # Remove trailing ] if present (from JSON parsing)
                    if doc_name.endswith("]"):
                        doc_name = doc_name[:-1].strip()
                    
                    # Find the field by label to get doctype
                    link_doctype = None
                    for field in meta.fields:
                        if field.label == field_label and field.fieldtype == "Link":
                            link_doctype = field.options
                            break
                    
                    if link_doctype and doc_name:
                        missing_docs.append({
                            "doctype": link_doctype,
                            "name": doc_name,
                            "field_label": field_label
                        })
    
    if not missing_docs:
        return False
    
    # Try to create each missing document
    created_count = 0
    for missing_doc in missing_docs:
        link_doctype = missing_doc["doctype"]
        link_name = missing_doc["name"]
        field_label = missing_doc["field_label"]
        
        
        # Try to get from local first
        doc_created = False
        try:
            local_doc = frappe.get_doc(link_doctype, link_name)
            # Document exists locally
            if direction == "send":
                # Sync it to remote
                try:
                    sync_result = sync_document_to_remote(
                        link_doctype, link_name, target_url,
                        api_key, api_secret, force_create=True,
                        sync_method="Auto", settings=settings
                    )
                    if sync_result.get("status") == "success":
                        doc_created = True
                        created_count += 1
                except Exception as sync_error:
                    frappe.log_error(
                        title="Failed to sync missing document to remote",
                        message=f"Could not sync {link_doctype} {link_name} to remote: {str(sync_error)}"
                    )
            else:  # fetch direction - document already exists locally
                doc_created = True
                created_count += 1
        except frappe.DoesNotExistError:
            # Document doesn't exist locally, try to fetch from remote
            if direction == "fetch":
                try:
                    # Import here to avoid circular dependency
                    from havano_sync.havano_sync.tasks.fetch_operations import fetch_document_from_remote
                    fetch_result = fetch_document_from_remote(link_doctype, link_name)
                    if fetch_result.get("status") == "success":
                        doc_created = True
                        created_count += 1
                except Exception as fetch_error:
                    frappe.log_error(
                        title="Failed to fetch missing document from remote",
                        message=f"Could not fetch {link_doctype} {link_name} from remote: {str(fetch_error)}"
                    )
            else:  # send direction - document doesn't exist locally or remote
                # For master doctypes, create a minimal document
                master_doctypes = {'Company', 'Account', 'Cost Center', 'Warehouse', 'Currency', 'UOM', 'Item Group'}
                if link_doctype in master_doctypes:
                    try:
                        # Create minimal document locally first
                        minimal_doc = create_minimal_master_document(link_doctype, link_name)
                        if minimal_doc:
                            # Now sync it to remote
                            try:
                                sync_result = sync_document_to_remote(
                                    link_doctype, link_name, target_url,
                                    api_key, api_secret, force_create=True,
                                    sync_method="Auto", settings=settings
                                )
                                if sync_result.get("status") == "success":
                                    doc_created = True
                                    created_count += 1
                            except Exception as sync_error:
                                frappe.log_error(
                                    title="Failed to sync minimal master document to remote",
                                    message=f"Could not sync minimal {link_doctype} {link_name} to remote: {str(sync_error)}"
                                )
                    except Exception as create_error:
                        frappe.log_error(
                            title="Failed to create minimal master document",
                            message=f"Could not create minimal {link_doctype} {link_name}: {str(create_error)}"
                        )
                else:
                    frappe.log_error(
                        title="Missing document not found",
                        message=f"Missing document {link_doctype} {link_name} does not exist locally or on remote. Cannot create."
                    )
        
        if not doc_created:
            pass
    
    if created_count > 0:
        return True
    
    return False


def resolve_remote_document_name_by_sync_reference(
    api_client: Any,
    doctype: str,
    local_name: str
) -> Optional[str]:
    """
    Resolve the remote document name using sync_reference field.
    This is the preferred method for finding remote documents when local names have -Local suffix.
    
    Args:
        api_client: SyncAPI client instance
        doctype: Document type
        local_name: Local document name (may have -Local suffix)
    
    Returns:
        Remote document name if found, None otherwise
    """
    try:
        # Use custom API endpoint to find document by sync_reference
        # This works around the limitation that frappe.client.get_list doesn't allow custom fields in filters
        remote_name = api_client.find_document_by_sync_reference(doctype, local_name, sync_type="Local")
        if remote_name:
            return remote_name
        
        # Fallback: try to get the document by name directly (in case names match)
        try:
            remote_doc = api_client.get_document(doctype, local_name)
            # Document exists with same name, check if sync_reference matches
            if remote_doc.get('sync_reference') == local_name and remote_doc.get('sync_type') == 'Local':
                return local_name
        except (DocumentNotFoundError, requests.exceptions.HTTPError):
            # Document doesn't exist with that name
            pass
        except Exception as e:
            pass
    except Exception as e:
        pass
    
    return None


def sync_linked_documents(
    doc: "Document",
    api_client: Any,
    settings: Any,
    target_url: str,
    api_key: str,
    api_secret: str,
    synced_docs: Optional[set] = None,
    direction: str = "send",
    link_field_mapping: Optional[dict] = None
) -> dict:
    """
    Recursively sync linked documents that are referenced by the current document
    This ensures all dependencies exist on the remote server before syncing the main document
    
    Args:
        doc: The document to check for linked documents
        api_client: SyncAPI client instance
        settings: Havano Sync Settings
        target_url: Remote server URL
        api_key: API key for authentication
        api_secret: API secret for authentication
        synced_docs: Set of (doctype, name) tuples already synced to prevent infinite loops
        direction: "send" (to remote) or "fetch" (from remote)
        link_field_mapping: Dictionary to store mappings of (doctype, local_name) -> remote_name
    
    Returns:
        Dictionary mapping (doctype, local_name) -> remote_name for all synced linked documents
    """
    # Import here to avoid circular dependency
    from havano_sync.havano_sync.tasks.sync_operations import sync_document_to_remote
    
    if synced_docs is None:
        synced_docs = set()
    if link_field_mapping is None:
        link_field_mapping = {}
    
    # Get enabled doctypes for sync based on direction
    enabled_doctypes = set()
    if not settings:
        settings = get_sync_settings()
    if settings and hasattr(settings, 'syncable_doctypes'):
        for syncable in settings.syncable_doctypes:
            # Check if the appropriate direction is enabled
            if direction == "send":
                enabled = cint(syncable.get('send', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'send', 0))
            else:  # fetch
                enabled = cint(syncable.get('fetch', 0)) if hasattr(syncable, 'get') else cint(getattr(syncable, 'fetch', 0))
            
            if enabled:
                # Use 'doctypes' field name from child table
                doctype_name = syncable.get('doctypes') if hasattr(syncable, 'get') else getattr(syncable, 'doctypes', None)
                if doctype_name:
                    # Remove -Local suffix if present (doctype names should never have -Local suffix)
                    if doctype_name.endswith("-Local"):
                        original_doctype = doctype_name
                        doctype_name = doctype_name[:-6]  # Remove "-Local" (6 characters)
                    enabled_doctypes.add(doctype_name)
    
    # Track link fields in the document and their remote names
    link_fields = []
    link_field_mapping = {}  # Maps (doctype, local_name) -> remote_name
    
    # Get link fields from meta
    for field in doc.meta.fields:
        if field.fieldtype == "Link" and field.options:
            link_doctype = field.options
            link_value = doc.get(field.fieldname)
            
            # Skip None, null, empty string, or the string "None"
            if not link_value or link_value in (None, "", "None", "null"):
                continue
            
            # Exempted doctypes that should never be synced (auto-generated, ledger entries, etc.)
            exempted_doctypes = {
                'GL Entry', 'Stock Ledger Entry', 'Payment Ledger Entry', 'Repost Payment Ledger',
                'User', 'Error Log', 'Activity Log', 'Comment', 'Version', 'Communication',
                'Email Queue', 'Email Queue Recipient', 'Notification Log',
                'Scheduled Job Log', 'Scheduled Job Type', 'DocType',
                'Route History', 'Webform', 'Access Log', 'Portal Settings'
            }
            
            # Skip exempted doctypes
            if link_doctype in exempted_doctypes:
                continue
            
            # Sync linked documents if they exist and are either enabled OR are critical dependencies
            # Critical dependencies include: Account, Cost Center, Warehouse, Customer, Supplier, etc.
            critical_doctypes = {
                'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group',
                'Customer', 'Supplier', 'Item', 'Price List', 'Territory', 'Sales Person'
            }
            should_sync = link_value and (link_doctype in enabled_doctypes or link_doctype in critical_doctypes)
            
            if should_sync:
                # Check if this linked document exists on remote
                # Use sync_reference to find the remote document name (preferred method)
                exists_on_remote = False
                remote_name = link_value  # Default to local name
                
                # First, try to resolve using sync_reference (most reliable)
                resolved_remote_name = resolve_remote_document_name_by_sync_reference(
                    api_client, link_doctype, link_value
                )
                if resolved_remote_name:
                    exists_on_remote = True
                    remote_name = resolved_remote_name
            else:
                # Fallback: Try checking with the local name directly
                try:
                    api_client.get_document(link_doctype, link_value)
                    # Document exists with same name
                    exists_on_remote = True
                    remote_name = link_value
                except (DocumentNotFoundError, requests.exceptions.HTTPError):
                    # Document doesn't exist - will need to sync it
                    exists_on_remote = False
                except Exception as e:
                    # Other error occurred - log but continue
                    exists_on_remote = False
                
                # If document doesn't exist on remote, try to sync/fetch it
                if not exists_on_remote:
                    # Check if we've already synced this document in this recursion
                    doc_key = (link_doctype, link_value)
                    if doc_key not in synced_docs:
                        synced_docs.add(doc_key)
                        
                        # Try to get the linked document locally first
                        exists_locally = False
                        linked_doc = None
                        try:
                            linked_doc = frappe.get_doc(link_doctype, link_value)
                            exists_locally = True
                        except frappe.DoesNotExistError:
                            exists_locally = False
                        
                        if exists_locally:
                            # Document exists locally but not on remote - sync it
                            try:
                                # Recursively sync this linked document first (to handle its dependencies)
                                child_mapping = sync_linked_documents(
                                    linked_doc, api_client, settings, 
                                    target_url, api_key, api_secret, synced_docs, direction=direction,
                                    link_field_mapping=link_field_mapping
                                ) or {}
                                link_field_mapping.update(child_mapping)
                                
                                # Now sync the linked document itself to remote
                                if direction == "send":
                                    sync_result = sync_document_to_remote(
                                        link_doctype, link_value, target_url, 
                                        api_key, api_secret, force_create=True,
                                        sync_method="Auto", settings=settings
                                    )
                                    # Get the remote name from sync result or resolve using sync_reference
                                    if sync_result and sync_result.get("status") == "success":
                                        # Try to resolve the actual remote name using sync_reference (most reliable)
                                        remote_doc_name = resolve_remote_document_name_by_sync_reference(
                                            api_client, link_doctype, link_value
                                        ) or sync_result.get("name") or link_value
                                        # Store the mapping for updating document data later
                                        link_field_mapping[(link_doctype, link_value)] = remote_doc_name
                                    else:
                                        # If sync failed, try to resolve existing remote name, otherwise use local name
                                        remote_doc_name = resolve_remote_document_name_by_sync_reference(
                                            api_client, link_doctype, link_value
                                        ) or link_value
                                        link_field_mapping[(link_doctype, link_value)] = remote_doc_name
                                else:  # fetch direction - but document exists locally, so no need to fetch
                                    pass
                            except Exception as sync_error:
                                frappe.log_error(
                                    title="Failed to sync linked document",
                                    message=f"Could not sync linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name}: {str(sync_error)}"
                                )
                        else:
                            # Document doesn't exist locally either
                            # For fetch direction, try to fetch it from remote
                            if direction == "fetch":
                                try:
                                    # Import here to avoid circular dependency
                                    from havano_sync.havano_sync.tasks.fetch_operations import fetch_document_from_remote
                                    fetch_result = fetch_document_from_remote(link_doctype, link_value)
                                    if fetch_result.get("status") == "success":
                                         pass
                                    else:
                                        frappe.log_error(
                                            title="Failed to fetch linked document from remote",
                                            message=f"Could not fetch linked document {link_doctype} {link_value} from remote: {fetch_result.get('message', 'Unknown error')}"
                                        )
                                except Exception as fetch_error:
                                    frappe.log_error(
                                        title="Failed to fetch linked document from remote",
                                        message=f"Could not fetch linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name}: {str(fetch_error)}"
                                    )
                            else:  # send direction
                                # Document doesn't exist locally or on remote - can't sync it
                                frappe.log_error(
                                    title="Linked document not found",
                                    message=f"Linked document {link_doctype} {link_value} referenced by {doc.doctype} {doc.name} does not exist locally or on remote. Cannot sync."
                                )
                                pass
    
    # Also check child table link fields
    for field in doc.meta.fields:
        if field.fieldtype == "Table" and field.fieldname in doc.as_dict():
            child_table = doc.get(field.fieldname)
            if child_table:
                child_meta = frappe.get_meta(field.options)
                for child_row in child_table:
                    for child_field in child_meta.fields:
                        if child_field.fieldtype == "Link" and child_field.options:
                            link_doctype = child_field.options
                            link_value = child_row.get(child_field.fieldname)
                            
                            # Skip None, null, empty string, or the string "None"
                            if not link_value or link_value in (None, "", "None", "null"):
                                continue
                            
                            # Exempted doctypes that should never be synced (auto-generated, ledger entries, etc.)
                            exempted_doctypes = {
                                'GL Entry', 'Stock Ledger Entry', 'Payment Ledger Entry', 'Repost Payment Ledger',
                                'User', 'Error Log', 'Activity Log', 'Comment', 'Version', 'Communication',
                                'Email Queue', 'Email Queue Recipient', 'Notification Log',
                                'Scheduled Job Log', 'Scheduled Job Type', 'DocType',
                                'Route History', 'Webform', 'Access Log', 'Portal Settings'
                            }
                            
                            # Skip exempted doctypes
                            if link_doctype in exempted_doctypes:
                                continue
                            
                            # Sync linked documents if they exist and are either enabled OR are critical dependencies
                            critical_doctypes = {
                                'Account', 'Cost Center', 'Warehouse', 'Company', 'Currency', 'UOM', 'Item Group',
                                'Customer', 'Supplier', 'Item', 'Price List', 'Territory', 'Sales Person'
                            }
                            should_sync = link_value and (link_doctype in enabled_doctypes or link_doctype in critical_doctypes)
                            
                            if should_sync:
                                # Check if this linked document exists on remote
                                exists_on_remote = False
                                try:
                                    api_client.get_document(link_doctype, link_value)
                                    exists_on_remote = True
                                except (DocumentNotFoundError, requests.exceptions.HTTPError) as e:
                                    exists_on_remote = False
                                except Exception as e:
                                    exists_on_remote = False
                                
                                # If document doesn't exist on remote, try to sync/fetch it
                                if not exists_on_remote:
                                    doc_key = (link_doctype, link_value)
                                    if doc_key not in synced_docs:
                                        synced_docs.add(doc_key)
                                        
                                        # Try to get the linked document locally first
                                        exists_locally = False
                                        linked_doc = None
                                        try:
                                            linked_doc = frappe.get_doc(link_doctype, link_value)
                                            exists_locally = True
                                        except frappe.DoesNotExistError:
                                            exists_locally = False
                                        
                                        if exists_locally:
                                            # Document exists locally but not on remote - sync it
                                            try:
                                                child_mapping = sync_linked_documents(
                                                    linked_doc, api_client, settings,
                                                    target_url, api_key, api_secret, synced_docs, direction=direction,
                                                    link_field_mapping=link_field_mapping
                                                ) or {}
                                                link_field_mapping.update(child_mapping)
                                                if direction == "send":
                                                    sync_document_to_remote(
                                                        link_doctype, link_value, target_url,
                                                        api_key, api_secret, force_create=True,
                                                        sync_method="Auto", settings=settings
                                                    )
                                                else:  # fetch direction - but document exists locally
                                                    pass
                                            except Exception as sync_error:
                                                frappe.log_error(
                                                    title="Failed to sync linked document from child table",
                                                    message=f"Could not sync linked document {link_doctype} {link_value} from {field.fieldname} in {doc.doctype} {doc.name}: {str(sync_error)}"
                                                )
                                        else:
                                            # Document doesn't exist locally either
                                            if direction == "fetch":
                                                try:
                                                    # Import here to avoid circular dependency
                                                    from havano_sync.havano_sync.tasks.fetch_operations import fetch_document_from_remote
                                                    fetch_result = fetch_document_from_remote(link_doctype, link_value)
                                                    if fetch_result.get("status") == "success":
                                                         pass
                                                except Exception as fetch_error:
                                                    frappe.log_error(
                                                        title="Failed to fetch linked document from child table",
                                                        message=f"Could not fetch linked document {link_doctype} {link_value} from {field.fieldname}: {str(fetch_error)}"
                                                    )
                                            else:  # send direction
                                                frappe.log_error(
                                                    title="Linked document from child table not found",
                                                    message=f"Linked document {link_doctype} {link_value} from {field.fieldname} in {doc.doctype} {doc.name} does not exist locally or on remote"
                                                )
    
    # Return the mapping of local names to remote names
    return link_field_mapping

