"""
Common types for scrapers.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class BillStatement(BaseModel):
    """Represents a bill statement."""
    
    date: datetime
    due_date: datetime
    amount: float
    pdf_url: str
    account: str
    provider: str = "optimum"
    
    @property
    def idempotency_key(self) -> str:
        """Generate idempotency key for this statement."""
        return f"{self.provider}:{self.account}:{self.date.strftime('%Y-%m-%d')}"


class ScraperResult(BaseModel):
    """Result of a scraper operation."""
    
    success: bool
    statements: list[BillStatement] = []
    errors: list[str] = []
    files_downloaded: int = 0

