"""Schemas for DeepSeek LLM batch evaluation requests and structured responses."""

from typing import List, Optional
from pydantic import BaseModel, Field


class CommentBatchItem(BaseModel):
    """Single comment inside a micro-batch payload sent to DeepSeek."""
    id: str = Field(..., description="Internal comment ID or batch index")
    text: str = Field(..., description="Comment text")


class CommentVerdict(BaseModel):
    """Individual verdict returned by DeepSeek for a single comment."""
    id: str = Field(..., description="ID matching the input comment batch item")
    on_topic: bool = Field(..., description="True if referencing government policies, regulations, rules, or state bodies")
    severely_negative: bool = Field(..., description="True if expressing extreme negativity, strong hostility, or fierce opposition")
    severity_score: int = Field(..., ge=0, le=10, description="Severity score from 0 (harmless/off-topic) to 10 (extreme hostility/unrest)")
    reason: Optional[str] = Field(
        default="Evaluated",
        description="Concise 1-sentence English explanation of the score for every comment (0 to 10)",
    )


class BatchEvaluationResponse(BaseModel):
    """Structured JSON response from DeepSeek API."""
    results: List[CommentVerdict] = Field(default_factory=list)
