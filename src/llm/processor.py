"""Micro-batch processor for classifying comments with zero-cost pre-filtering."""

import re
import json
from typing import List, Optional, Tuple, Any
from sqlalchemy.ext.asyncio import AsyncSession
import structlog
from src.config import settings
from src.schemas.llm import CommentBatchItem, CommentVerdict
from src.schemas.alert import AlertPayload
from src.db.models import Comment, PlatformAuthor
from src.db.repositories.comment_repo import CommentRepository
from src.db.repositories.author_repo import AuthorRepository
from src.llm.client import DeepSeekClassifier

logger = structlog.get_logger(__name__)

# Regular expressions for zero-cost pre-filtering
EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\ud83c-\ud83e][\ud000-\udfff]",
    flags=re.UNICODE,
)
TEXT_SMILIKS_PATTERN = re.compile(r"^[\s\)\]\(\[:;DdxpP_~*!?.+\-]+$")
TRIGGER_WORDS_SHORT = {
    "istefa", "iste'fo", "otstavka", "отставка", "boycott", "boykot",
    "etiraz", "protest", "inqilob", "revolyutsiya", "революция",
}


def is_trivial_comment(text: str) -> bool:
    """Identify if a comment is trivial, emoji-only, smiliks, or short 1-3 words without substance."""
    if not text:
        return True
    cleaned = text.strip()
    if len(cleaned) < 3:
        return True

    # 1. Check if it's pure text smiliks like ))), (((, :D, :-), etc.
    if TEXT_SMILIKS_PATTERN.match(cleaned):
        return True

    # 2. Strip URLs and @mentions
    stripped = re.sub(r"https?://\S+|@\w+", "", cleaned).strip()
    if not stripped:
        return True

    # 3. Strip emojis
    no_emoji = EMOJI_PATTERN.sub("", stripped).strip()
    if not no_emoji or TEXT_SMILIKS_PATTERN.match(no_emoji):
        return True

    # 4. Count substantive alphanumeric words
    words = [w for w in re.split(r"[\s,.-]+", no_emoji) if any(c.isalnum() for c in w)]

    # Ignore short 1, 2, or 3-word comments (e.g. "zor gap yoq", "ha shunday", "yaxshi aka")
    if len(words) <= 3:
        # Only keep if an explicit direct hostile trigger word exists
        has_trigger = any(w.lower() in TRIGGER_WORDS_SHORT for w in words)
        if not has_trigger:
            return True

    return False


class MicroBatchProcessor:
    """Manages comment batching, zero-cost pre-filtering, and DeepSeek dispatch."""

    def __init__(
        self,
        classifier: Optional[DeepSeekClassifier] = None,
        batch_size: Optional[int] = None,
        alert_threshold: Optional[int] = None,
    ):
        self.classifier = classifier or DeepSeekClassifier()
        self.batch_size = batch_size or settings.LLM_BATCH_SIZE
        self.alert_threshold = alert_threshold or settings.LLM_ALERT_SEVERITY_THRESHOLD

    async def process_batch(
        self,
        session: AsyncSession,
        redis_client: Optional[Any] = None,
    ) -> Tuple[int, List[AlertPayload]]:
        """Pull pending comments, pre-filter, micro-batch evaluate, and stage alerts.

        Returns:
            Tuple of (evaluated_count, list_of_alerts)
        """
        comment_repo = CommentRepository(session)
        author_repo = AuthorRepository(session)

        pending = await comment_repo.get_pending_for_llm(limit=self.batch_size)
        if not pending:
            return 0, []

        valid_items: List[CommentBatchItem] = []
        pending_map = {c.id: c for c in pending}

        # Stage 1: Zero-cost pre-filter
        for comment in pending:
            if is_trivial_comment(comment.content_text):
                await comment_repo.mark_skipped_prefilter(
                    comment.id,
                    reason="Skipped pre-filter: trivial, emoji-only, or under 2 words",
                )
            else:
                valid_items.append(CommentBatchItem(id=comment.id, text=comment.content_text))

        if not valid_items:
            await session.commit()
            return len(pending), []

        # Mark items as processing
        await comment_repo.set_status_processing([item.id for item in valid_items])
        await session.commit()

        # Stage 2: DeepSeek Batch Evaluation
        alerts: List[AlertPayload] = []
        try:
            verdicts, _ = await self.classifier.evaluate_batch(valid_items)
            verdict_dict = {v.id: v for v in verdicts}

            for item in valid_items:
                comment = pending_map.get(item.id)
                if not comment:
                    continue

                verdict = verdict_dict.get(item.id)
                if not verdict:
                    # Fallback verdict if LLM missed an ID in the batch
                    verdict = CommentVerdict(
                        id=item.id,
                        on_topic=False,
                        severely_negative=False,
                        severity_score=0,
                        reason="Unclassified fallback",
                    )

                await comment_repo.update_llm_verdict(
                    comment_id=item.id,
                    is_on_topic=verdict.on_topic,
                    is_severely_negative=verdict.severely_negative,
                    severity_score=verdict.severity_score,
                    llm_reason=verdict.reason,
                    llm_status="evaluated",
                )

                # Check if it meets severe negative alert threshold
                if verdict.severely_negative and verdict.severity_score >= self.alert_threshold:
                    # Update author flagged count
                    if comment.author_id:
                        await author_repo.increment_comment_count(comment.author_id, is_flagged=True)

                    # Build alert payload
                    author_user_id = "Unknown"
                    author_handle = None
                    author_name = None
                    if comment.author:
                        author_user_id = comment.author.external_user_id
                        author_handle = comment.author.username
                        author_name = comment.author.display_name

                    alert = AlertPayload(
                        comment_id=comment.id,
                        platform=comment.platform,
                        source_name=comment.source_name or "Unknown Source",
                        post_url=comment.post_url,
                        comment_url=comment.comment_url,
                        author_external_id=author_user_id,
                        author_username=author_handle,
                        author_display_name=author_name,
                        content_text=comment.content_text,
                        severity_score=verdict.severity_score,
                        llm_reason=verdict.reason or "Severely negative regulatory comment",
                    )
                    alerts.append(alert)

                    # Push to Redis alert queue if client provided
                    if redis_client:
                        try:
                            await redis_client.rpush("alert_queue", alert.model_dump_json())
                        except Exception as r_err:
                            logger.error("Failed to enqueue alert to Redis", error=str(r_err))

            await session.commit()
            return len(pending), alerts

        except Exception as exc:
            logger.error("Failed micro-batch evaluation", error=str(exc))
            # Revert processing status to pending on error
            for item in valid_items:
                await comment_repo.update_llm_verdict(
                    comment_id=item.id,
                    is_on_topic=False,
                    is_severely_negative=False,
                    severity_score=0,
                    llm_status="failed",
                    llm_reason=f"Evaluation failed: {str(exc)[:200]}",
                )
            await session.commit()
            return len(pending), []
