"""Unified schema for normalized comments ingested from any platform."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class NormalizedComment(BaseModel):
    """Standardized representation of an ingested message/comment across all 5 platforms."""

    platform: str = Field(..., description="Platform identifier: telegram, youtube, instagram, facebook, x")
    external_comment_id: str = Field(..., description="Platform-native comment/message ID")
    parent_external_id: Optional[str] = Field(None, description="Parent post or comment ID")
    child_comment_count: int = Field(default=0, description="Count of nested reply comments")
    
    # Target / Source info
    target_external_id: Optional[str] = Field(None, description="Chat ID, Video ID, Post ID")
    source_name: Optional[str] = Field(None, description="Group/Channel/Page/Account title")
    post_url: Optional[str] = Field(None, description="Direct URL to original post or video")
    comment_url: Optional[str] = Field(None, description="Direct deep link to the exact comment")
    
    # Author identity
    author_external_id: str = Field(..., description="Platform numeric user ID or unique identifier")
    author_username: Optional[str] = Field(None, description="Handle without @")
    author_display_name: Optional[str] = Field(None, description="Full name or display name")
    author_profile_url: Optional[str] = Field(None, description="Link to profile")
    
    # Content
    content_text: str = Field(..., description="Raw text of the comment")
    published_at: Optional[datetime] = Field(None, description="Datetime when comment was posted")
