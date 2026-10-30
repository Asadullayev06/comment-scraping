"""Micro-batch processor for classifying comments with zero-cost pre-filtering and copypasta deduplication."""

import re
from typing import List, Optional, Tuple, Any, Dict
from sqlalchemy import select
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


def normalize_text_key(text: str) -> str:
    """Normalize comment text for exact/near-exact copypasta deduplication."""
    return " ".join((text or "").lower().split())


def is_trivial_comment(text: str) -> bool:
    """Identify if a comment is trivial, emoji-only, sticker/smiliks, or short 1-4 words without substance.

    Note: Trivial comments are still stored in PostgreSQL for total volume statistics,
    but are marked `skipped_prefilter` so they never consume DeepSeek LLM tokens.
    """
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
    words = [w for w in re.split(r"[\s,.\-!?;:()\"']+", no_emoji) if any(c.isalnum() for c in w)]

    # Ignore short 1 to 4-word comments (e.g. "zor gap yoq", "ha shunday boladi", "yaxshi rahmat aka")
    if len(words) <= 4:
        # Only keep if an explicit direct hostile trigger word exists
        has_trigger = any(w.lower() in TRIGGER_WORDS_SHORT for w in words)
        if not has_trigger:
            return True

    return False


class MicroBatchProcessor:
    """Manages comment batching, zero-cost pre-filtering, copypasta deduplication, and DeepSeek dispatch."""

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
        """Pull pending comments, pre-filter (<=4 words/emojis), deduplicate copypasta, evaluate, and stage alerts.

        Returns:
            Tuple of (evaluated_count, list_of_alerts)
        """
        comment_repo = CommentRepository(session)
        author_repo = AuthorRepository(session)

        pending = await comment_repo.get_pending_for_llm(limit=self.batch_size)
        if not pending:
            return 0, []

        pending_map = {c.id: c for c in pending}

        # Group non-trivial comments by normalized text so copypasta is only scored once
        norm_groups: Dict[str, List[Comment]] = {}
        for comment in pending:
            if is_trivial_comment(comment.content_text):
                await comment_repo.mark_skipped_prefilter(
                    comment.id,
                    reason="Skipped pre-filter: emoji, sticker, or short sentence (<= 4 words)",
                )
            else:
                norm_key = normalize_text_key(comment.content_text)
                norm_groups.setdefault(norm_key, []).append(comment)

        if not norm_groups:
            await session.commit()
            return len(pending), []

        # Stage 1.5: Check if any normalized copypasta text was already evaluated in PostgreSQL
        cached_verdicts: Dict[str, CommentVerdict] = {}
        unique_items_for_llm: List[CommentBatchItem] = []
        rep_id_to_norm_key: Dict[str, str] = {}

        for norm_key, group_comments in norm_groups.items():
            rep_comment = group_comments[0]
            existing_stmt = (
                select(Comment)
                .where(
                    Comment.content_text == rep_comment.content_text,
                    Comment.llm_status == "evaluated",
                    Comment.severity_score.is_not(None),
                )
                .limit(1)
            )
            existing_eval = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing_eval is not None:
                score = existing_eval.severity_score or 0
                cached_verdicts[norm_key] = CommentVerdict(
                    id=rep_comment.id,
                    on_topic=bool(existing_eval.is_on_topic),
                    severely_negative=bool(score >= self.alert_threshold),
                    severity_score=score,
                    reason=existing_eval.llm_reason or f"Cached duplicate verdict (score {score}/10)",
                )
            else:
                unique_items_for_llm.append(CommentBatchItem(id=rep_comment.id, text=rep_comment.content_text))
                rep_id_to_norm_key[rep_comment.id] = norm_key

        all_valid_ids = [c.id for group in norm_groups.values() for c in group]
        await comment_repo.set_status_processing(all_valid_ids)
        await session.commit()

        # Stage 2: DeepSeek Batch Evaluation (only for unique non-cached texts)
        alerts: List[AlertPayload] = []
        try:
            if unique_items_for_llm:
                verdicts, _ = await self.classifier.evaluate_batch(unique_items_for_llm)
                for v in verdicts:
                    n_key = rep_id_to_norm_key.get(v.id)
                    if n_key:
                        cached_verdicts[n_key] = v

            # Apply verdicts across all comments in each normalized text group
            for norm_key, group_comments in norm_groups.items():
                verdict = cached_verdicts.get(norm_key)
                if not verdict:
                    verdict = CommentVerdict(
                        id=group_comments[0].id,
                        on_topic=False,
                        severely_negative=False,
                        severity_score=0,
                        reason="Unclassified fallback",
                    )

                # Single source of truth: flagged is strictly derived from severity_score >= threshold
                score = int(verdict.severity_score or 0)
                is_flagged = bool(score >= self.alert_threshold)
                reason_text = (verdict.reason or "").strip() or f"Evaluated with severity score {score}/10."

                for idx, comment in enumerate(group_comments):
                    await comment_repo.update_llm_verdict(
                        comment_id=comment.id,
                        is_on_topic=bool(verdict.on_topic),
                        is_severely_negative=is_flagged,
                        severity_score=score,
                        llm_reason=reason_text,
                        llm_status="evaluated",
                    )

                    # Only dispatch real-time Telegram alert once per unique copypasta text in a batch
                    if is_flagged and idx == 0:
                        if comment.author_id:
                            await author_repo.increment_comment_count(comment.author_id, is_flagged=True)

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
                            severity_score=score,
                            llm_reason=reason_text,
                        )
                        alerts.append(alert)

                        if redis_client:
                            try:
                                await redis_client.rpush("alert_queue", alert.model_dump_json())
                            except Exception as r_err:
                                logger.error("Failed to enqueue alert to Redis", error=str(r_err))

            await session.commit()
            return len(pending), alerts

        except Exception as exc:
            logger.error("Failed micro-batch evaluation", error=str(exc))
            for cid in all_valid_ids:
                await comment_repo.update_llm_verdict(
                    comment_id=cid,
                    is_on_topic=False,
                    is_severely_negative=False,
                    severity_score=0,
                    llm_status="failed",
                    llm_reason=f"Evaluation failed: {str(exc)[:200]}",
                )
            await session.commit()
            return len(pending), []
