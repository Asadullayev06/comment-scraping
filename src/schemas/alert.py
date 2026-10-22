"""Schema for alert payloads dispatched to the Telegram Bot."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class AlertPayload(BaseModel):
    """Payload representing a severely negative comment ready for Telegram alert delivery."""

    comment_id: str
    platform: str
    source_name: Optional[str] = "Unknown Source"
    post_url: Optional[str] = None
    comment_url: Optional[str] = None

    # Author
    author_external_id: str
    author_username: Optional[str] = None
    author_display_name: Optional[str] = None

    # Content & Evaluation
    content_text: str
    severity_score: int
    llm_reason: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
