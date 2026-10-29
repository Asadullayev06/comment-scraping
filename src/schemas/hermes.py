"""Lightweight intelligence digest schema for Hermes Agent hand-off.

CRITICAL ARCHITECTURAL CONTRACT:
Never pass hundreds of NormalizedComment dictionaries into Hermes's context window
(500 raw comments = ~150 KB = ~38,000 tokens per turn).
Instead, scrapers and the micro-batch pipeline return a compact ScrapeRunSummary
containing aggregate counts and ONLY the items flagged with severe hostility (score >= threshold).
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class ThreatSummaryItem(BaseModel):
    """Compact representation of a single high-threat comment for Hermes."""
    id: str = Field(..., description="Internal comment UUID")
    author_username: Optional[str] = Field(None, description="Author handle")
    author_display_name: Optional[str] = Field(None, description="Author display name")
    platform: str = Field(..., description="Platform identifier: telegram, instagram, youtube, x, facebook")
    severity_score: int = Field(..., ge=7, le=10, description="Hostility severity score (7 to 10)")
    llm_reason: str = Field(..., description="1-sentence explanation of why it was flagged")
    content_text: str = Field(..., description="Full text of the hostile comment")
    comment_url: Optional[str] = Field(None, description="Direct deep link to the comment")


class ScrapeRunSummary(BaseModel):
    """Token-efficient execution summary passed back to Hermes."""
    target_id: str = Field(..., description="Target UUID or shortcode/chat ID")
    platform: str = Field(..., description="Platform monitored")
    total_comments_scanned: int = Field(default=0, description="Total comments processed in this run")
    newly_ingested: int = Field(default=0, description="Count of new comments stored in database")
    noise_skipped_count: int = Field(default=0, description="Count of trivial comments dropped (emojis, 1-2 words)")
    threats_detected_count: int = Field(default=0, description="Count of high-threat comments flagged")
    threats: List[ThreatSummaryItem] = Field(default_factory=list, description="List of high-threat comments only")
