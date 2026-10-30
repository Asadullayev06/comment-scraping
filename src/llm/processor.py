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

# Short abuse, insult, profanity, corruption, and protest lexicon (Uzbek, Russian, Azerbaijani, Turkish, English)
# Comments of 1-4 words matching any of these stems/words bypass the pre-filter and go to DeepSeek.
SHORT_ABUSE_EXACT = {
    "vor", "вор", "гад", "qul", "mol", "it", "poy", "lo'x", "lox", "лох",
}

SHORT_ABUSE_STEMS = (
    # Uzbek insults, abuse, corruption, and regime hostility
    "jinni", "harom", "xarom", "poraxo", "ogri", "o'g'ri", "ugri", "firibgar",
    "muttaham", "yolgon", "yolg'on", "axmoq", "ahmoq", "tupoy", "iflos", "maraz",
    "vatanfurush", "xoin", "zolim", "zulm", "qaroqchi", "talon", "lanat", "la'nat",
    "yoqol", "yo'qol", "diktator", "korrup", "namard", "pastkash", "hayvon", "eshak",
    "chochqa", "cho'chqa", "sharmanda", "masxara", "kloun", "kazzob", "ochkoz",
    "ochko'z", "olim", "o'lim", "isyon", "qozgolon", "qo'zg'olon", "istefa", "iste'fo",
    "boykot", "boycott", "protest", "etiraz", "inqilob", "revolyut", "qallob",
    "yulgich", "yulg'ich", "tekinxor", "tekinxo'r", "axlat", "najosat", "foxisha",
    "fohisha", "jalab", "kaltak", "urish", "yoqish", "portlat",
    # Russian insults, abuse, corruption, and hostility
    "воры", "воров", "корруп", "взятк", "мошенн", "грабеж", "грабят", "грабит",
    "позор", "тварь", "твари", "урод", "мраз", "скотин", "сволоч", "тупой", "тупые",
    "идиот", "дебил", "дурак", "клоун", "диктат", "тиран", "фашист", "предател",
    "враг", "ложь", "лжец", "врань", "отставк", "митинг", "протест", "бойкот",
    "бунт", "долой", "ненавиж", "проклят", "убийц", "ублюд", "шлюх", "сука", "суки",
    "блять", "бляд", "пизд", "хуй", "нахуй", "ебат", "заеб", "пидор", "пидар", "чмо",
    "дармоед", "паразит", "кровопийц", "оккупант", "революц",
    # Transliterated Russian / Mixed
    "pozor", "mraz", "tvar", "urod", "idiot", "debil", "durak", "svoloch", "otstavk",
    "miting", "bunt", "doloy", "ublyud", "pidor", "pidar", "blat", "blyat", "suka",
    # Azerbaijani & Turkish insults, abuse, corruption, and hostility
    "ogru", "oğru", "rusvet", "rüşvət", "rusvay", "rüsvay", "haram", "eclaf", "əclaf",
    "alcaq", "alçaq", "serefsiz", "şərəfsiz", "biqeyret", "biqeyrət", "vicdansiz",
    "vicdansız", "yalanci", "yalançı", "firildaq", "fırıldaq", "soygun", "soyğun",
    "zulm", "zülm", "xain", "axmaq", "deli", "dəli", "heyvan", "kopek", "köpək",
    "donuz", "lenet", "lənət", "redd", "rədd", "cindir", "cındır", "qehbe", "qəhbə",
    "ogras", "oğraş", "peysər", "peyser", "sikdir", "siktir", "yobaz", "istifa",
)


def _normalize_apostrophes(word: str) -> str:
    """Normalize Uzbek/Turkic apostrophe variants and strip punctuation."""
    return re.sub(r"[’‘`ʻʼ']", "'", word.lower()).strip("'")


def _has_short_abuse_or_trigger(words: List[str]) -> bool:
    """Check if any word in a short 1-4 word comment matches the abuse/insult/protest lexicon."""
    for raw_w in words:
        w = _normalize_apostrophes(raw_w)
        w_no_apos = w.replace("'", "")
        if not w:
            continue
        if w in SHORT_ABUSE_EXACT or w_no_apos in SHORT_ABUSE_EXACT:
            return True
        for stem in SHORT_ABUSE_STEMS:
            stem_no_apos = stem.replace("'", "")
            if w.startswith(stem) or w_no_apos.startswith(stem_no_apos):
                return True
            if len(stem_no_apos) >= 4 and (stem in w or stem_no_apos in w_no_apos):
                return True
    return False


def normalize_text_key(text: str) -> str:
    """Normalize comment text for exact/near-exact copypasta deduplication."""
    return " ".join((text or "").lower().split())


def is_trivial_comment(text: str) -> bool:
    """Identify if a comment is trivial, emoji-only, sticker/smiliks, or short 1-4 words without abuse/substance.

    Note: Trivial comments are still stored in PostgreSQL for total volume statistics,
    but are marked `skipped_prefilter` so they never consume DeepSeek LLM tokens.
    Short 1-4 word insults/abuse (e.g., "jinni", "harom", "o'g'rilar") bypass this filter via lexicon match.
    """
    if not text:
        return True
    cleaned = text.strip()
    if len(cleaned) < 2:
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

    # 4. Extract alphanumeric words (preserving internal apostrophes for Uzbek words like o'g'ri)
    words = [
        w for w in re.split(r"[\s,.\-!?;:()\"\u00ab\u00bb]+", no_emoji)
        if any(c.isalnum() for c in w)
    ]
    if not words:
        return True

    # Ignore short 1 to 4-word benign comments unless they match the abuse/insult/protest lexicon
    if len(words) <= 4:
        if _has_short_abuse_or_trigger(words):
            return False
        if len(cleaned) < 3:
            return True
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
        target_id: Optional[str] = None,
        include_failed: bool = True,
    ) -> Tuple[int, List[AlertPayload]]:
        """Pull pending comments, pre-filter (<=4 words/emojis), deduplicate copypasta, evaluate, and stage alerts.

        Returns:
            Tuple of (evaluated_count, list_of_alerts)
        """
        comment_repo = CommentRepository(session)
        author_repo = AuthorRepository(session)

        pending = await comment_repo.get_pending_for_llm(
            limit=self.batch_size,
            target_id=target_id,
            include_failed=include_failed,
        )
        if not pending:
            return 0, []

        # Pre-snapshot plain Python attributes while ORM objects & eager-loaded authors are in-memory
        snapshots: Dict[str, Dict[str, Any]] = {}
        for c in pending:
            auth_obj = c.author
            snapshots[c.id] = {
                "id": c.id,
                "content_text": c.content_text or "",
                "author_id": c.author_id,
                "author_external_id": auth_obj.external_user_id if auth_obj else "Unknown",
                "author_username": auth_obj.username if auth_obj else None,
                "author_display_name": auth_obj.display_name if auth_obj else None,
                "platform": c.platform,
                "source_name": c.source_name or "Unknown Source",
                "post_url": c.post_url,
                "comment_url": c.comment_url,
            }

        # Group non-trivial comments by normalized text so copypasta is only scored once
        norm_groups: Dict[str, List[Dict[str, Any]]] = {}
        for c_id, snap in snapshots.items():
            if is_trivial_comment(snap["content_text"]):
                await comment_repo.mark_skipped_prefilter(
                    c_id,
                    reason="Skipped pre-filter: emoji, sticker, or benign short phrase (<= 4 words)",
                )
            else:
                norm_key = normalize_text_key(snap["content_text"])
                norm_groups.setdefault(norm_key, []).append(snap)

        if not norm_groups:
            await session.commit()
            return len(pending), []

        # Stage 1.5: Check if any normalized copypasta text was already evaluated in PostgreSQL
        cached_verdicts: Dict[str, CommentVerdict] = {}
        unique_items_for_llm: List[CommentBatchItem] = []
        rep_id_to_norm_key: Dict[str, str] = {}

        for norm_key, group_snaps in norm_groups.items():
            rep_snap = group_snaps[0]
            existing_stmt = (
                select(Comment)
                .where(
                    Comment.content_text == rep_snap["content_text"],
                    Comment.llm_status == "evaluated",
                    Comment.severity_score.is_not(None),
                )
                .limit(1)
            )
            existing_eval = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing_eval is not None:
                score = int(existing_eval.severity_score)
                cached_verdicts[norm_key] = CommentVerdict(
                    id=rep_snap["id"],
                    on_topic=bool(existing_eval.is_on_topic),
                    severely_negative=bool(score >= self.alert_threshold),
                    severity_score=score,
                    reason=existing_eval.llm_reason or f"Cached duplicate verdict (score {score}/10)",
                )
            else:
                unique_items_for_llm.append(CommentBatchItem(id=rep_snap["id"], text=rep_snap["content_text"]))
                rep_id_to_norm_key[rep_snap["id"]] = norm_key

        all_valid_ids = [snap["id"] for group in norm_groups.values() for snap in group]
        await comment_repo.set_status_processing(all_valid_ids)
        await session.flush()

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
            for norm_key, group_snaps in norm_groups.items():
                verdict = cached_verdicts.get(norm_key)
                if not verdict:
                    # Never write score=0 on missing verdict; record status='failed' and score=NULL for retry
                    for snap in group_snaps:
                        await comment_repo.update_llm_verdict(
                            comment_id=snap["id"],
                            is_on_topic=None,
                            is_severely_negative=False,
                            severity_score=None,
                            llm_reason="Evaluation failed: missing verdict from LLM batch response",
                            llm_status="failed",
                        )
                    continue

                # Single source of truth: flagged is strictly derived from severity_score >= threshold
                score = int(verdict.severity_score if verdict.severity_score is not None else 0)
                is_flagged = bool(score >= self.alert_threshold)
                reason_text = (verdict.reason or "").strip() or f"Evaluated with severity score {score}/10."

                for idx, snap in enumerate(group_snaps):
                    await comment_repo.update_llm_verdict(
                        comment_id=snap["id"],
                        is_on_topic=bool(verdict.on_topic),
                        is_severely_negative=is_flagged,
                        severity_score=score,
                        llm_reason=reason_text,
                        llm_status="evaluated",
                    )

                    # Only dispatch real-time Telegram alert once per unique copypasta text in a batch
                    if is_flagged and idx == 0:
                        if snap["author_id"]:
                            await author_repo.increment_comment_count(snap["author_id"], is_flagged=True)

                        alert = AlertPayload(
                            comment_id=snap["id"],
                            platform=snap["platform"],
                            source_name=snap["source_name"],
                            post_url=snap["post_url"],
                            comment_url=snap["comment_url"],
                            author_external_id=snap["author_external_id"],
                            author_username=snap["author_username"],
                            author_display_name=snap["author_display_name"],
                            content_text=snap["content_text"],
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
                    is_on_topic=None,
                    is_severely_negative=False,
                    severity_score=None,
                    llm_status="failed",
                    llm_reason=f"Evaluation failed: {str(exc)[:200]}",
                )
            await session.commit()
            return 0, []

    async def process_all_pending(
        self,
        session: AsyncSession,
        target_id: Optional[str] = None,
        redis_client: Optional[Any] = None,
        max_batches: int = 100,
    ) -> Tuple[int, List[AlertPayload]]:
        """Loop micro-batch evaluator until all unscored comments (for a target or globally) are processed."""
        total_processed = 0
        all_alerts: List[AlertPayload] = []
        for idx in range(max_batches):
            # Include failed rows on the first pass so transient failures are retried once per run
            batch_count, batch_alerts = await self.process_batch(
                session=session,
                redis_client=redis_client,
                target_id=target_id,
                include_failed=(idx == 0),
            )
            if batch_count == 0:
                break
            total_processed += batch_count
            all_alerts.extend(batch_alerts)
        return total_processed, all_alerts
