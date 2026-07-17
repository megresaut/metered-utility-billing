"""
HTTP client for API interactions.
"""

import logging
import requests
from pathlib import Path
from typing import Optional, Dict, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config

logger = logging.getLogger(__name__)


class APIClient:
    """Client for interacting with the RA API."""
    
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        
        # Configure retries
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Set headers
        self.session.headers.update({
            "Authorization": f"Bearer {config.ra_api_token}",
            "Content-Type": "application/json"
        })
    
    def upload_file(self, file_path: Path, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Upload a file to the scraper/files endpoint."""
        url = f"{self.config.ra_api_base}/scraper/files"
        
        try:
            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f, 'application/pdf')}
                data = metadata or {}
                
                response = self.session.post(url, files=files, data=data)
                response.raise_for_status()
                
                logger.info(f"Successfully uploaded file: {file_path.name}")
                return response.json()
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to upload file {file_path}: {e}")
            raise
    
    def submit_bill(self, bill_data: Dict[str, Any]) -> Dict[str, Any]:
        """Submit bill data to the scraper/bills endpoint."""
        url = f"{self.config.ra_api_base}/scraper/bills"
        
        try:
            response = self.session.post(url, json=bill_data)
            response.raise_for_status()
            
            logger.info(f"Successfully submitted bill: {bill_data.get('idempotency_key')}")
            return response.json()
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to submit bill: {e}")
            raise

