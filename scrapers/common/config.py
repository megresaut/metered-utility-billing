"""
Configuration management for scrapers.
"""

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class Config(BaseModel):
    """Scraper configuration."""
    
    # API Configuration
    ra_api_base: str = Field(default="http://localhost:8080", env="RA_API_BASE")
    ra_api_token: str = Field(env="RA_API_TOKEN")
    
    # Optimum Configuration
    optimum_username: str = Field(env="OPTIMUM_USERNAME")
    optimum_password: str = Field(env="OPTIMUM_PASSWORD")
    optimum_account: str = Field(env="OPTIMUM_ACCOUNT")
    
    # Scraper Configuration
    out_dir: Path = Field(default=Path("./out/optimum"), env="OUT_DIR")
    dry_run: bool = Field(default=False, env="DRY_RUN")
    test_only: bool = Field(default=False, env="TEST_ONLY")
    headless: bool = Field(default=True, env="HEADLESS")
    
    def __init__(self, **data):
        super().__init__(**data)
        # Ensure output directory exists
        self.out_dir.mkdir(parents=True, exist_ok=True)
    
    def __str__(self) -> str:
        """String representation excluding sensitive data."""
        return (
            f"Config(ra_api_base={self.ra_api_base}, "
            f"optimum_account={self.optimum_account}, "
            f"out_dir={self.out_dir}, dry_run={self.dry_run})"
        )
