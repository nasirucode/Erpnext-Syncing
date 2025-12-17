# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from typing import Optional, Dict, Any


class DocumentNotFoundError(Exception):
	"""Exception raised when a document is not found on the remote server"""
	pass


class DuplicateEntryError(Exception):
	"""Exception raised when a document already exists on the remote server (duplicate entry)"""
	pass


class SyncAPI:
	"""
	Handle API communication for syncing between local and cloud instances
	
	Uses admin_api_key and admin_api_secret from Havano Sync Settings
	for authentication with the remote Frappe instance.
	"""
	
	def __init__(self, base_url: str, api_key: str, api_secret: str):
		"""
		Initialize API client
		
		Args:
			base_url: Remote server URL
			api_key: Admin API Key from Havano Sync Settings (admin_api_key field)
			api_secret: Admin API Secret from Havano Sync Settings (admin_api_secret field)
		"""
		self.base_url = base_url.rstrip('/')
		# Strip whitespace from credentials to ensure proper formatting
		# These are admin_api_key and admin_api_secret from Havano Sync Settings
		self.api_key = api_key.strip() if api_key else api_key
		self.api_secret = api_secret.strip() if api_secret else api_secret
		self.session = requests.Session()
	
	def _get_headers(self) -> Dict[str, str]:
		"""
		Get authentication headers for API requests
		
		Uses Frappe's token authentication format: 'token api_key:api_secret'
		where api_key is admin_api_key and api_secret is admin_api_secret
		from Havano Sync Settings.
		
		All endpoints use this header format for authentication.
		"""
		return {
			"Authorization": f"token {self.api_key}:{self.api_secret}",
			"Content-Type": "application/json",
			"Accept": "application/json"
		}
	
	def _make_request(
		self, 
		method: str, 
		endpoint: str, 
		data: Optional[Dict[str, Any]] = None,
		params: Optional[Dict[str, Any]] = None
	) -> Dict[str, Any]:
		"""
		Make an API request to the remote instance
		All requests use Authorization header: 'token api_key:api_secret'
		"""
		url = f"{self.base_url}/api/method/{endpoint}"
		headers = self._get_headers()  # All endpoints use this header format
		
		try:
			if method.upper() == "GET":
				response = self.session.get(url, headers=headers, params=params, timeout=30)
			elif method.upper() == "POST":
				response = self.session.post(url, headers=headers, json=data, timeout=30)
			elif method.upper() == "PUT":
				response = self.session.put(url, headers=headers, json=data, timeout=30)
			elif method.upper() == "PATCH":
				response = self.session.patch(url, headers=headers, json=data, timeout=30)
			else:
				raise ValueError(f"Unsupported HTTP method: {method}")
			
			response.raise_for_status()
			result = response.json()
			# Frappe API returns data in 'message' key
			if isinstance(result, dict) and 'message' in result:
				return result['message']
			return result
		
		except requests.exceptions.HTTPError as e:
			# Try to get error details from response
			error_details = {}
			remote_error = None
			is_expected_error = False
			if e.response is not None:
				try:
					error_response = e.response.json()
					error_details = error_response
					# Frappe error messages can be in various keys
					if 'exc' in error_response:
						remote_error = error_response['exc']
						error_details['frappe_error'] = remote_error
					elif 'exception' in error_response:
						remote_error = error_response['exception']
						error_details['frappe_error'] = remote_error
					elif 'message' in error_response:
						remote_error = error_response['message']
						error_details['frappe_message'] = remote_error
					elif 'error' in error_response:
						remote_error = error_response['error']
						error_details['frappe_error'] = remote_error
					# Check if this is an expected error (404/DoesNotExistError)
					# This is normal when checking if a document exists
					# Treat all 404 errors as expected to suppress error messages
					if e.response.status_code == 404:
						is_expected_error = True
					# Try to get traceback if available
					if 'traceback' in error_response:
						error_details['frappe_traceback'] = error_response['traceback']
				except Exception as parse_error:
					# If JSON parsing fails, get raw text
					try:
						raw_text = e.response.text[:1000]
						error_details = {'raw_response': raw_text}
						# Try to extract error message from HTML/text
						if 'Traceback' in raw_text or 'Error' in raw_text:
							remote_error = raw_text
					except:
						error_details = {'parse_error': str(parse_error)}
			
			# Only log unexpected errors (404/DoesNotExistError is expected when checking document existence)
			if not is_expected_error:
				log_message = f"Error making {method} request to {url}\nStatus: {e.response.status_code if e.response else 'unknown'}\n"
				if remote_error:
					log_message += f"Remote server error: {remote_error}\n"
				log_message += f"Error details: {json.dumps(error_details, indent=2)}"
				
				frappe.log_error(
					title="Sync API Request Failed",
					message=log_message
				)
			
			# Re-raise with more details
			error_msg = str(e)
			if remote_error:
				error_msg = f"{error_msg}\nRemote server error: {remote_error}"
				# Check for specific error types and provide clearer messages
				if "NestedSetRecursionError" in str(remote_error) or "cannot be added to its own descendants" in str(remote_error):
					error_msg = f"Validation Error: The document has a circular parent-child relationship. {error_msg}"
				elif "LinkValidationError" in str(remote_error) or "Could not find" in str(remote_error):
					error_msg = f"Link Validation Error: Referenced document not found on remote server. {error_msg}"
				elif "TimestampMismatchError" in str(remote_error):
					error_msg = f"Conflict Error: Document was modified on remote server. {error_msg}"
			raise requests.exceptions.HTTPError(error_msg, response=e.response)
		
		except requests.exceptions.RequestException as e:
			frappe.log_error(
				title="Sync API Request Failed",
				message=f"Error making {method} request to {url}: {str(e)}"
			)
			raise
	
	def submit_document(self, doctype: str, name: str) -> Dict[str, Any]:
		"""
		Submit a document on the remote instance
		"""
		endpoint = "frappe.client.submit"
		url = f"{self.base_url}/api/method/{endpoint}"
		headers = self._get_headers()
		headers["Content-Type"] = "application/x-www-form-urlencoded"
		
		try:
			data = {
				"doctype": doctype,
				"name": name
			}
			response = self.session.post(
				url,
				headers=headers,
				data=data,
				timeout=30
			)
			response.raise_for_status()
			result = response.json()
			if isinstance(result, dict) and 'message' in result:
				return result['message']
			return result
		except requests.exceptions.HTTPError as e:
			error_details = {}
			remote_error = None
			if e.response:
				try:
					error_response = e.response.json()
					remote_error = error_response.get('exc') or error_response.get('_server_messages')
					if remote_error:
						if isinstance(remote_error, str):
							try:
								remote_error = json.loads(remote_error)[0]  # Extract actual error message
							except:
								pass
					error_details = error_response
				except:
					pass
			
			log_message = f"Error submitting document {doctype} {name} on remote.\nStatus: {e.response.status_code if e.response else 'unknown'}\n"
			if remote_error:
				log_message += f"Remote server error: {remote_error}\n"
			log_message += f"Error details: {json.dumps(error_details, indent=2)}"
			
			frappe.log_error(
				title="Sync API Submit Failed",
				message=log_message
			)
			raise requests.exceptions.HTTPError(f"Failed to submit document on remote: {remote_error or str(e)}", response=e.response)
	
	def rename_document(self, doctype: str, old_name: str, new_name: str, merge: bool = False, force: bool = True) -> Dict[str, Any]:
		"""
		Rename a document on the remote instance using Frappe API v2
		Endpoint: /api/v2/document/{doctype}/{name}/method/rename?name=newname
		
		Args:
			doctype: Document type
			old_name: Current document name on remote
			new_name: New document name (should match local name with -Local suffix)
			merge: Whether to merge if target name exists (default: False)
			force: Whether to force rename (default: True)
		"""
		# Use API v2 endpoint: /api/v2/document/{doctype}/{name}/method/rename?name=newname
		url = f"{self.base_url}/api/v2/document/{doctype}/{old_name}/method/rename"
		headers = self._get_headers()
		
		try:
			# API v2 uses query parameters for rename
			params = {
				"name": new_name
			}
			# Add optional parameters if needed
			if force:
				params["force"] = "1"
			if merge:
				params["merge"] = "1"
			
			response = self.session.post(url, headers=headers, params=params, timeout=30)
			response.raise_for_status()
			result = response.json()
			# API v2 returns data directly or in 'data' key
			if isinstance(result, dict):
				if 'data' in result:
					return result['data']
				elif 'message' in result:
					return result['message']
			return result
		except requests.exceptions.HTTPError as e:
			# Handle errors similar to create_document
			error_details = {}
			remote_error = None
			if e.response:
				try:
					error_response = e.response.json()
					if isinstance(error_response, dict) and 'exc_type' in error_response:
						remote_error = error_response.get('exc', '')
						error_details = error_response
					elif isinstance(error_response, dict) and 'error' in error_response:
						remote_error = error_response.get('error', '')
						error_details = error_response
				except:
					pass
			error_msg = f"Failed to rename {doctype} from {old_name} to {new_name}"
			if remote_error:
				error_msg = f"{error_msg}\nRemote error: {remote_error}"
			frappe.log_error(
				title="Rename Document Failed",
				message=error_msg
			)
			raise requests.exceptions.HTTPError(error_msg, response=e.response)
	
	def create_document(self, doctype: str, doc: Dict[str, Any], ignore_validate: bool = False, ignore_permissions: bool = False) -> Dict[str, Any]:
		"""
		Create a document on the remote instance
		frappe.client.insert expects the document data to be passed as 'doc' parameter
		
		Args:
			doctype: Document type
			doc: Document data dictionary
			ignore_validate: If True, bypass validation (default: False)
			ignore_permissions: If True, bypass permission checks (default: False)
		"""
		endpoint = "frappe.client.insert"
		# Ensure doctype is set
		if 'doctype' not in doc:
			doc['doctype'] = doctype
		
		# frappe.client.insert expects the document as 'doc' parameter
		# Send as form-encoded data with 'doc' as JSON string (same format as save)
		url = f"{self.base_url}/api/method/{endpoint}"
		headers = self._get_headers()
		# Change Content-Type to application/x-www-form-urlencoded for form data
		headers["Content-Type"] = "application/x-www-form-urlencoded"
		
		try:
			# Build form data
			form_data = {"doc": json.dumps(doc)}
			if ignore_validate:
				form_data["ignore_validate"] = "1"
			if ignore_permissions:
				form_data["ignore_permissions"] = "1"
			
			# Send doc as JSON string in form data
			response = self.session.post(
				url, 
				headers=headers, 
				data=form_data, 
				timeout=30
			)
			response.raise_for_status()
			result = response.json()
			# Frappe API returns data in 'message' key
			if isinstance(result, dict) and 'message' in result:
				return result['message']
			return result
		except requests.exceptions.HTTPError as e:
			# Use the same error handling as _make_request
			error_details = {}
			remote_error = None
			is_expected_error = False
			if e.response is not None:
				try:
					error_response = e.response.json()
					error_details = error_response
					if 'exc' in error_response:
						remote_error = error_response['exc']
						error_details['frappe_error'] = remote_error
					elif 'exception' in error_response:
						remote_error = error_response['exception']
						error_details['frappe_error'] = remote_error
					elif 'message' in error_response:
						remote_error = error_response['message']
						error_details['frappe_message'] = remote_error
					elif 'error' in error_response:
						remote_error = error_response['error']
						error_details['frappe_error'] = remote_error
					
					# Check for duplicate entry error - this is expected, document already exists
					if 'exc_type' in error_response and 'DuplicateEntryError' in error_response.get('exc_type', ''):
						is_expected_error = True
						# Raise special exception for duplicate entries
						raise DuplicateEntryError(f"Document already exists on remote server: {remote_error or str(e)}")
					
					# Check for UniqueValidationError (417 status code) - often for sync_reference duplicates
					if e.response.status_code == 417:
						if 'exc_type' in error_response and 'UniqueValidationError' in error_response.get('exc_type', ''):
							is_expected_error = True
							# Check if it's a sync_reference duplicate
							if 'sync_reference' in str(remote_error).lower() or 'sync_reference' in str(error_response):
								raise DuplicateEntryError(f"Document with same sync_reference already exists on remote server: {remote_error or str(e)}")
							else:
								# Other unique validation error
								raise DuplicateEntryError(f"Unique validation error on remote server: {remote_error or str(e)}")
					
					if e.response.status_code == 404:
						if 'exc_type' in error_response and 'DoesNotExistError' in error_response.get('exc_type', ''):
							is_expected_error = True
					if 'traceback' in error_response:
						error_details['frappe_traceback'] = error_response['traceback']
				except DuplicateEntryError:
					# Re-raise duplicate entry errors
					raise
				except Exception as parse_error:
					try:
						raw_text = e.response.text[:1000]
						error_details = {'raw_response': raw_text}
						if 'Traceback' in raw_text or 'Error' in raw_text:
							remote_error = raw_text
						# Check for duplicate in raw text
						if "Duplicate entry" in raw_text or "already exists" in raw_text.lower() or "DuplicateEntryError" in raw_text:
							raise DuplicateEntryError(f"Document already exists on remote server: {raw_text}")
					except DuplicateEntryError:
						raise
					except:
						error_details = {'parse_error': str(parse_error)}
			
			if not is_expected_error:
				log_message = f"Error making POST request to {url}\nStatus: {e.response.status_code if e.response else 'unknown'}\n"
				if remote_error:
					log_message += f"Remote server error: {remote_error}\n"
				log_message += f"Error details: {json.dumps(error_details, indent=2)}"
				
				frappe.log_error(
					title="Sync API Request Failed",
					message=log_message
				)
			
			error_msg = str(e)
			if remote_error:
				error_msg = f"{error_msg}\nRemote server error: {remote_error}"
				# Check for specific error types and provide clearer messages
				if "NestedSetRecursionError" in str(remote_error) or "cannot be added to its own descendants" in str(remote_error):
					error_msg = f"Validation Error: The document has a circular parent-child relationship. {error_msg}"
				elif "LinkValidationError" in str(remote_error) or "Could not find" in str(remote_error):
					error_msg = f"Link Validation Error: Referenced document not found on remote server. {error_msg}"
				elif "TimestampMismatchError" in str(remote_error):
					error_msg = f"Conflict Error: Document was modified on remote server. {error_msg}"
			
			# Check for 403 Forbidden error and provide helpful message
			if e.response and e.response.status_code == 403:
				error_msg = (
					f"Permission Denied (403 Forbidden): The API user associated with the Admin API Key does not have permission to create/update documents on the remote server.\n\n"
					f"To fix this issue:\n"
					f"1. Go to the remote Frappe instance (https://getpos.havano.cloud)\n"
					f"2. Navigate to User List and find the user associated with the Admin API Key\n"
					f"3. Ensure the user has the 'System Manager' role OR has appropriate permissions for the doctype '{doctype}'\n"
					f"4. Check the doctype permissions in Settings > Permissions for '{doctype}'\n"
					f"5. Verify the API Key and API Secret are correct in Havano Sync Settings\n\n"
					f"Original error: {error_msg}"
				)
				# Log this as a critical error with clear instructions
				frappe.log_error(
					title=f"Sync Permission Denied: {doctype}",
					message=error_msg
				)
			
			raise requests.exceptions.HTTPError(error_msg, response=e.response)
	
	def update_document(self, doctype: str, name: str, doc: Dict[str, Any]) -> Dict[str, Any]:
		"""
		Update a document on the remote instance
		frappe.client.save expects the document data to be passed as 'doc' parameter
		To avoid timestamp mismatch errors, we fetch the latest version first and merge changes
		"""
		endpoint = "frappe.client.save"
		
		# First, fetch the latest version to get the current modified timestamp
		# This prevents TimestampMismatchError
		try:
			latest_doc = self.get_document(doctype, name)
			# Merge our changes into the latest version
			# Start with the latest doc (preserves metadata like modified timestamp)
			# Then apply our field changes on top
			merged_doc = latest_doc.copy()
			# Apply our field changes (excluding metadata fields)
			metadata_fields = {'name', 'doctype', 'modified', 'modified_by', 'creation', 'owner', '_user_tags', '_comments', '_assign', '_liked_by', '_seen'}
			for key, value in doc.items():
				if not key.startswith('_') and key not in metadata_fields:
					merged_doc[key] = value
			# Preserve the latest modified timestamp and other metadata
			# (already in merged_doc from latest_doc.copy())
			doc = merged_doc
		except DocumentNotFoundError:
			# Document doesn't exist, will create it instead
			pass
		
		# Ensure name and doctype are set
		doc['name'] = name
		doc['doctype'] = doctype
		
		# frappe.client.save expects the document as 'doc' parameter
		# Send as form-encoded data with 'doc' as JSON string
		url = f"{self.base_url}/api/method/{endpoint}"
		headers = self._get_headers()
		# Change Content-Type to application/x-www-form-urlencoded for form data
		headers["Content-Type"] = "application/x-www-form-urlencoded"
		
		try:
			# Send doc as JSON string in form data
			response = self.session.post(
				url, 
				headers=headers, 
				data={"doc": json.dumps(doc)}, 
				timeout=30
			)
			response.raise_for_status()
			result = response.json()
			# Frappe API returns data in 'message' key
			if isinstance(result, dict) and 'message' in result:
				return result['message']
			return result
		except requests.exceptions.HTTPError as e:
			# Use the same error handling as _make_request
			error_details = {}
			remote_error = None
			is_expected_error = False
			if e.response is not None:
				try:
					error_response = e.response.json()
					error_details = error_response
					if 'exc' in error_response:
						remote_error = error_response['exc']
						error_details['frappe_error'] = remote_error
					elif 'exception' in error_response:
						remote_error = error_response['exception']
						error_details['frappe_error'] = remote_error
					elif 'message' in error_response:
						remote_error = error_response['message']
						error_details['frappe_message'] = remote_error
					elif 'error' in error_response:
						remote_error = error_response['error']
						error_details['frappe_error'] = remote_error
					if e.response.status_code == 404:
						if 'exc_type' in error_response and 'DoesNotExistError' in error_response.get('exc_type', ''):
							is_expected_error = True
					if 'traceback' in error_response:
						error_details['frappe_traceback'] = error_response['traceback']
				except Exception as parse_error:
					try:
						raw_text = e.response.text[:1000]
						error_details = {'raw_response': raw_text}
						if 'Traceback' in raw_text or 'Error' in raw_text:
							remote_error = raw_text
					except:
						error_details = {'parse_error': str(parse_error)}
			
			if not is_expected_error:
				log_message = f"Error making POST request to {url}\nStatus: {e.response.status_code if e.response else 'unknown'}\n"
				if remote_error:
					log_message += f"Remote server error: {remote_error}\n"
				log_message += f"Error details: {json.dumps(error_details, indent=2)}"
				
				frappe.log_error(
					title="Sync API Request Failed",
					message=log_message
				)
			
			error_msg = str(e)
			if remote_error:
				error_msg = f"{error_msg}\nRemote server error: {remote_error}"
				# Check for specific error types and provide clearer messages
				if "NestedSetRecursionError" in str(remote_error) or "cannot be added to its own descendants" in str(remote_error):
					error_msg = f"Validation Error: The document has a circular parent-child relationship. {error_msg}"
				elif "LinkValidationError" in str(remote_error) or "Could not find" in str(remote_error):
					error_msg = f"Link Validation Error: Referenced document not found on remote server. {error_msg}"
				elif "TimestampMismatchError" in str(remote_error):
					error_msg = f"Conflict Error: Document was modified on remote server. {error_msg}"
			raise requests.exceptions.HTTPError(error_msg, response=e.response)
	
	def get_document(self, doctype: str, name: str) -> Dict[str, Any]:
		"""
		Get a document from the remote instance
		Raises HTTPError with 404 if document doesn't exist (expected behavior)
		"""
		endpoint = f"frappe.client.get"
		params = {
			"doctype": doctype,
			"name": name
		}
		try:
			return self._make_request("GET", endpoint, params=params)
		except requests.exceptions.HTTPError as e:
			# 404 means document doesn't exist - this is expected, suppress error message
			if e.response and e.response.status_code == 404:
				# Document doesn't exist - raise a specific exception that can be caught silently
				# Use a simple message without details to prevent error messages from being displayed
				raise DocumentNotFoundError("Document not found")
			# Re-raise other errors
			raise
	
	def check_document_exists(self, doctype: str, name: str) -> bool:
		"""Check if a document exists on the remote instance"""
		try:
			self.get_document(doctype, name)
			return True
		except DocumentNotFoundError:
			# Document doesn't exist - this is expected, not an error
			return False
		except:
			# Other errors - document might exist but there's a connection/auth issue
			return False
	
	def find_document_by_sync_reference(self, doctype: str, sync_reference: str, sync_type: str = "Local") -> Optional[str]:
		"""
		Find a document on the remote instance by sync_reference field.
		Uses standard Frappe API methods (does not use custom havano_sync API endpoint).
		
		Note: frappe.client.get_list doesn't support custom fields in filters,
		so we try to get the document by name (assuming sync_reference often matches the name).
		
		Args:
			doctype: Document type to search
			sync_reference: The sync_reference value to search for
			sync_type: The sync_type value (default: "Local")
		
		Returns:
			Document name if found, None otherwise
		"""
		# frappe.client.get_list doesn't support custom fields like sync_reference in filters
		# So we try to get the document by name (sync_reference value) and verify it matches
		try:
			doc = self.get_document(doctype, sync_reference)
			if doc and doc.get('sync_reference') == sync_reference and doc.get('sync_type') == sync_type:
				return sync_reference
		except (DocumentNotFoundError, requests.exceptions.HTTPError):
			# Document not found by name - sync_reference doesn't match the name
			pass
		except Exception as e:
			frappe.logger().debug(f"Error finding document by sync_reference for {doctype} {sync_reference}: {str(e)}")
		
		# If sync_reference doesn't match the name, we can't find it without custom API
		# Return None - the caller should handle this gracefully
		return None
	
	def test_connection(self):
		"""
		Test connection to the remote instance
		Returns: (success: bool, error_message: Optional[str])
		"""
		try:
			# Try a simple API endpoint to test connection
			endpoint = "frappe.auth.get_logged_user"
			response = self._make_request("GET", endpoint)
			return True, None
		except requests.exceptions.ConnectionError as e:
			error_msg = f"Connection error: Unable to reach {self.base_url}. Please check the URL and network connectivity."
			return False, error_msg
		except requests.exceptions.Timeout as e:
			error_msg = f"Connection timeout: The server at {self.base_url} did not respond in time."
			return False, error_msg
		except requests.exceptions.HTTPError as e:
			if e.response and e.response.status_code == 401:
				error_msg = "Authentication failed: Invalid API Key or API Secret."
			elif e.response and e.response.status_code == 403:
				error_msg = "Access denied: API Key or API Secret does not have required permissions."
			elif e.response and e.response.status_code == 404:
				error_msg = f"Endpoint not found: The server at {self.base_url} may not be a Frappe instance."
			else:
				error_msg = f"HTTP error {e.response.status_code if e.response else 'unknown'}: {str(e)}"
			return False, error_msg
		except Exception as e:
			error_msg = f"Connection test failed: {str(e)}"
			return False, error_msg
	
	def run_remote_migration(self):
		"""
		Run bench migrate on the remote instance
		Returns: (success: bool, message: str)
		"""
		try:
			endpoint = "havano_sync.havano_sync.api.sync.run_migration"
			response = self._make_request("POST", endpoint, data={})
			
			if response and isinstance(response, dict):
				if response.get("status") == "success":
					return True, response.get("message", "Migration completed successfully")
				else:
					return False, response.get("message", "Migration failed")
			else:
				return True, "Migration command executed (response format unknown)"
		except requests.exceptions.HTTPError as e:
			# Check if app is not installed
			error_text = ""
			error_text_lower = ""
			if e.response:
				try:
					error_response = e.response.json()
					# Handle different error response formats
					if 'message' in error_response:
						error_text = str(error_response['message'])
						# Handle list format ["Traceback..."]
						if isinstance(error_response['message'], list):
							error_text = " ".join(str(m) for m in error_response['message'])
					elif 'exc' in error_response:
						error_text = str(error_response['exc'])
						if isinstance(error_response['exc'], list):
							error_text = " ".join(str(ex) for ex in error_response['exc'])
					error_text_lower = error_text.lower()
				except:
					error_text = e.response.text[:1000] if e.response.text else ""
					error_text_lower = error_text.lower()
			
			# Check for app not installed error (check in both original and lower case)
			if ("app havano_sync is not installed" in error_text_lower or 
				"havano_sync is not installed" in error_text_lower or
				"appnotinstallederror" in error_text_lower):
				error_msg = "Havano Sync app is not installed on the remote server. Please install the app on the remote server to enable migration."
			elif e.response and e.response.status_code == 403:
				error_msg = "Permission denied: API user does not have permission to run migration. Please ensure the API user has 'System Manager' role."
			elif e.response and e.response.status_code == 404:
				error_msg = "Migration endpoint not found. Please ensure havano_sync app is installed on the remote server."
			elif e.response and e.response.status_code == 417:
				# 417 Expectation Failed - usually means app not installed or endpoint not available
				if "not installed" in error_text_lower:
					error_msg = "Havano Sync app is not installed on the remote server. Please install the app on the remote server to enable migration."
				else:
					error_msg = "Migration endpoint not available. Please ensure havano_sync app is installed on the remote server."
			else:
				error_msg = f"HTTP error {e.response.status_code if e.response else 'unknown'}: {str(e)}"
			return False, error_msg
		except Exception as e:
			error_msg = f"Failed to run migration: {str(e)}"
			return False, error_msg

