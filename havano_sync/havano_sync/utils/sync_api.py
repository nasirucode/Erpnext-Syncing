# Copyright (c) 2025, nasirucode and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from typing import Optional, Dict, Any


class SyncAPI:
	"""Handle API communication for syncing between local and cloud instances"""
	
	def __init__(self, base_url: str, api_key: str, api_secret: str):
		self.base_url = base_url.rstrip('/')
		self.api_key = api_key
		self.api_secret = api_secret
		self.session = requests.Session()
	
	def _get_headers(self) -> Dict[str, str]:
		"""Get authentication headers for API requests"""
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
		"""Make an API request to the remote instance"""
		url = f"{self.base_url}/api/method/{endpoint}"
		headers = self._get_headers()
		
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
		
		except requests.exceptions.RequestException as e:
			frappe.log_error(
				title="Sync API Request Failed",
				message=f"Error making {method} request to {url}: {str(e)}"
			)
			raise
	
	def create_document(self, doctype: str, doc: Dict[str, Any]) -> Dict[str, Any]:
		"""Create a document on the remote instance"""
		endpoint = "frappe.client.insert"
		# frappe.client.insert expects doc as direct parameter
		return self._make_request("POST", endpoint, data=doc)
	
	def update_document(self, doctype: str, name: str, doc: Dict[str, Any]) -> Dict[str, Any]:
		"""Update a document on the remote instance"""
		endpoint = "frappe.client.save"
		# frappe.client.save expects doc with name and doctype
		doc['name'] = name
		doc['doctype'] = doctype
		return self._make_request("POST", endpoint, data=doc)
	
	def get_document(self, doctype: str, name: str) -> Dict[str, Any]:
		"""Get a document from the remote instance"""
		endpoint = f"frappe.client.get"
		params = {
			"doctype": doctype,
			"name": name
		}
		return self._make_request("GET", endpoint, params=params)
	
	def check_document_exists(self, doctype: str, name: str) -> bool:
		"""Check if a document exists on the remote instance"""
		try:
			self.get_document(doctype, name)
			return True
		except:
			return False
	
	def test_connection(self) -> bool:
		"""Test connection to the remote instance"""
		try:
			endpoint = "frappe.auth.get_logged_user"
			response = self._make_request("GET", endpoint)
			return True
		except:
			return False

