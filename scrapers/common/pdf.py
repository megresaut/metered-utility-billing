"""
PDF text extraction utilities.
"""

import re
import logging
from pathlib import Path
from typing import List, Optional

import pdfplumber

logger = logging.getLogger(__name__)


def extract_amount_due(pdf_path: Path) -> int:
    """
    Extract the amount due from a PDF bill.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Amount in cents as integer (e.g., 123456 for $1,234.56)
        
    Raises:
        Exception: If PDF cannot be read or no amount found
    """
    logger.info(f"Extracting amount due from PDF: {pdf_path}")
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Extract all text from all pages
            full_text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"
            
            if not full_text:
                raise Exception("No text found in PDF")
            
            logger.debug(f"PDF text length: {len(full_text)} characters")
            
            # Look for "Amount Due" or "Total Amount Due" patterns
            amount_due_patterns = [
                r'(?:Amount Due|Total Amount Due|Amount\s+Due)\s*:?\s*\$?([0-9,]+\.?\d*)',
                r'(?:Amount Due|Total Amount Due|Amount\s+Due)\s*:?\s*([0-9,]+\.?\d*)',
                r'\$\s*([0-9,]+\.?\d*)\s*(?:Amount Due|Total Amount Due)',
            ]
            
            for pattern in amount_due_patterns:
                matches = re.findall(pattern, full_text, re.IGNORECASE)
                if matches:
                    amount_str = matches[0].replace(',', '')
                    try:
                        amount_float = float(amount_str)
                        amount_cents = int(amount_float * 100)
                        logger.info(f"Found Amount Due: ${amount_float:.2f} ({amount_cents} cents)")
                        return amount_cents
                    except ValueError:
                        continue
            
            # Fallback: find the largest currency value in the PDF
            logger.warning("Amount Due label not found, searching for largest currency value")
            currency_pattern = r'\$\s*([0-9,]+\.?\d*)'
            currency_matches = re.findall(currency_pattern, full_text)
            
            if not currency_matches:
                raise Exception("No currency values found in PDF")
            
            # Convert to floats and find the largest
            amounts = []
            for match in currency_matches:
                try:
                    amount_float = float(match.replace(',', ''))
                    amounts.append(amount_float)
                except ValueError:
                    continue
            
            if not amounts:
                raise Exception("No valid currency amounts found in PDF")
            
            largest_amount = max(amounts)
            amount_cents = int(largest_amount * 100)
            logger.info(f"Found largest currency value: ${largest_amount:.2f} ({amount_cents} cents)")
            return amount_cents
            
    except Exception as e:
        logger.error(f"Failed to extract amount from PDF {pdf_path}: {e}")
        raise

