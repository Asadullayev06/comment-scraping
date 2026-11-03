"""DeepSeek API Async Client with prefix caching optimization and resilience."""

import json
import asyncio
from typing import List, Optional, Tuple, Dict, Any
from openai import AsyncOpenAI
import structlog
from src.config import settings
from src.schemas.llm import CommentBatchItem, CommentVerdict, BatchEvaluationResponse
from src.llm.prompts import CLASSIFICATION_SYSTEM_PROMPT, VIDEO_ANALYSIS_SYSTEM_PROMPT

logger = structlog.get_logger(__name__)


class DeepSeekClassifier:
    """Async classifier client communicating with DeepSeek-V3 API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or settings.DEEPSEEK_API_KEY
        self.base_url = base_url or settings.DEEPSEEK_BASE_URL
        self.model = model or settings.DEEPSEEK_MODEL

        self.client = AsyncOpenAI(
            api_key=self.api_key or "mock-key",
            base_url=self.base_url,
            timeout=30.0,
        )

    async def evaluate_batch(
        self,
        comments: List[CommentBatchItem],
        max_retries: int = 3,
    ) -> Tuple[List[CommentVerdict], Dict[str, Any]]:
        """Evaluate a micro-batch of up to 40 comments using DeepSeek.

        Returns:
            Tuple of (verdicts_list, usage_metadata)
        """
        if not comments:
            return [], {}

        # If API key is not configured, run offline mock classification for testing
        if not self.api_key or self.api_key.startswith("sk-xxxx") or self.api_key == "mock-key":
            logger.warning("DeepSeek API key not configured; running heuristic mock evaluation")
            return self._heuristic_mock_evaluate(comments), {"mock": True}

        # Build dynamic payload strictly inside the user message
        user_content = json.dumps([item.model_dump() for item in comments], ensure_ascii=False)

        messages = [
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze the following comments and return the JSON results array:\n{user_content}"},
        ]

        attempt = 0
        backoff = 1.5

        while attempt < max_retries:
            attempt += 1
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )

                content = response.choices[0].message.content or "{}"
                parsed_json = json.loads(content)

                # Validate against Pydantic schema
                batch_response = BatchEvaluationResponse.model_validate(parsed_json)

                # Log prefix caching metrics if reported by DeepSeek
                usage = getattr(response, "usage", None)
                cache_hits = getattr(usage, "prompt_cache_hit_tokens", 0) if usage else 0
                cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) if usage else 0
                total_tokens = getattr(usage, "total_tokens", 0) if usage else 0

                logger.info(
                    "DeepSeek batch completed",
                    batch_size=len(comments),
                    cache_hit_tokens=cache_hits,
                    cache_miss_tokens=cache_miss,
                    total_tokens=total_tokens,
                )

                usage_meta = {
                    "cache_hit_tokens": cache_hits,
                    "cache_miss_tokens": cache_miss,
                    "total_tokens": total_tokens,
                }
                return batch_response.results, usage_meta

            except Exception as exc:
                logger.error(
                    "DeepSeek evaluation attempt failed",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(backoff)
                backoff *= 2.0

        return [], {}

    def _heuristic_mock_evaluate(self, comments: List[CommentBatchItem]) -> List[CommentVerdict]:
        """Offline fallback evaluator for unit tests or initial setup before API keys are added."""
        trigger_keywords = ["qanun", "vergi", "rejim", "soyğun", "etiraz", "boğur", "hükümet", "закон", "грабеж", "чиновники", "corruption", "tyranny", "protest"]
        verdicts = []
        for c in comments:
            lower = c.text.lower()
            hit = any(kw in lower for kw in trigger_keywords)
            if hit:
                verdicts.append(
                    CommentVerdict(
                        id=c.id,
                        on_topic=True,
                        severely_negative=True,
                        severity_score=8,
                        reason="Heuristic detected strong hostility or opposition toward government policy.",
                    )
                )
            else:
                verdicts.append(
                    CommentVerdict(
                        id=c.id,
                        on_topic=False,
                        severely_negative=False,
                        severity_score=1,
                        reason="Off-topic or benign everyday comment.",
                    )
                )
        return verdicts

    async def analyze_video(
        self,
        video_payload: Dict[str, Any],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Analyze a YouTube video's content/transcript (Option 1: mode='video') or video + audience reactions (Option 2: mode='reactions')."""
        if not self.api_key or self.api_key.startswith("sk-xxxx") or self.api_key == "mock-key":
            logger.warning("DeepSeek API key not configured; running heuristic mock video analysis")
            return self._heuristic_mock_analyze_video(video_payload)

        user_content = json.dumps(video_payload, ensure_ascii=False)
        messages = [
            {"role": "system", "content": VIDEO_ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze the following YouTube video data and return the JSON intelligence report:\n{user_content}"},
        ]

        attempt = 0
        backoff = 1.5
        while attempt < max_retries:
            attempt += 1
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content or "{}"
                parsed_json = json.loads(content)
                if not isinstance(parsed_json, dict):
                    parsed_json = {}
                return parsed_json
            except Exception as exc:
                logger.error("DeepSeek video analysis attempt failed", attempt=attempt, error=str(exc))
                if attempt >= max_retries:
                    return self._heuristic_mock_analyze_video(video_payload)
                await asyncio.sleep(backoff)
                backoff *= 2.0

        return self._heuristic_mock_analyze_video(video_payload)

    def _heuristic_mock_analyze_video(self, video_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Offline fallback video analyzer for unit tests or unconfigured API key."""
        mode = video_payload.get("mode") or "video"
        focus = video_payload.get("focus")
        v_meta = video_payload.get("video") or {}
        title = v_meta.get("title") or "YouTube Video"
        channel = v_meta.get("channel_name") or "Unknown Channel"
        transcript_text = (video_payload.get("transcript_text") or "").strip()
        desc = (v_meta.get("description") or "").strip()

        combined = f"{title} {desc} {transcript_text}".lower()
        trigger_keywords = ["qanun", "vergi", "rejim", "soyğun", "etiraz", "soliq", "jarima", "hokimiyat", "коррупция", "чиновники", "corruption", "tax", "tariff"]
        on_topic = any(kw in combined for kw in trigger_keywords)
        sev_score = 5 if on_topic else 1

        key_moments = []
        segments = video_payload.get("transcript_segments") or []
        for seg in segments[:3]:
            key_moments.append(
                {
                    "timestamp": seg.get("timestamp", "00:00"),
                    "topic": "Transcript segment",
                    "summary": seg.get("text", ""),
                }
            )

        audience_reaction = None
        if mode == "reactions":
            comments = video_payload.get("comments") or []
            hostile_samples = [
                f"@{c.get('author', 'user')}: {c.get('text', '')[:100]}"
                for c in comments
                if (c.get("severity_score") or 0) >= 7
            ][:3]
            all_samples = [
                f"@{c.get('author', 'user')}: {c.get('text', '')[:100]}"
                for c in comments[:3]
            ]
            audience_reaction = {
                "overall_sentiment": (
                    f"Analyzed {len(comments)} viewer comments; {len(hostile_samples)} flagged for high hostility."
                    if comments
                    else "No viewer comments available for this video."
                ),
                "agreement_with_video": "Viewers actively engage with the video's core topic and express strong opinions on the issues raised.",
                "narrative_divergence": "Commenters focus on practical policy impacts and accountability.",
                "dominant_viewer_grievances": ["Public policy implementation", "Official accountability"] if hostile_samples else ["General discussion"],
                "reactions_by_video_part": [
                    {
                        "video_part": f"{key_moments[0]['timestamp']} - {key_moments[0]['summary'][:60]}" if key_moments else f"00:00 - {title[:60]}",
                        "viewer_reaction": "Viewers reacted directly to this segment in the comment section.",
                        "sentiment": "hostile" if hostile_samples else "mixed",
                        "sample_comments": hostile_samples or all_samples,
                    }
                ] if (key_moments or comments) else [],
            }

        return {
            "summary": f"Video '{title}' by {channel}. " + (transcript_text[:200] if transcript_text else (desc[:200] or "No transcript text available.")),
            "language": video_payload.get("transcript_language") or "unknown",
            "on_topic": on_topic,
            "severity_score": sev_score,
            "severely_negative": sev_score >= 7,
            "tone_and_stance": "Critical commentary" if on_topic else "Informational / general content",
            "main_topics": (v_meta.get("tags") or [])[:5] or [title[:50]],
            "key_moments": key_moments,
            "claims_and_allegations": [],
            "risk_assessment": "Moderate policy discussion." if on_topic else "Low risk informational video.",
            "focused_answer": f"Focused analysis on '{focus}': addressed in video '{title}'." if focus else None,
            "audience_reaction": audience_reaction,
        }
