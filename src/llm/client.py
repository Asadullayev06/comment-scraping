"""DeepSeek API Async Client for comment classification and Google Gemini API Client for YouTube video analysis."""

import json
import asyncio
import re
from typing import List, Optional, Tuple, Dict, Any
import httpx
from openai import AsyncOpenAI
import structlog
from src.config import settings
from src.collectors.base import ScraperError
from src.schemas.llm import CommentBatchItem, CommentVerdict, BatchEvaluationResponse
from src.llm.prompts import CLASSIFICATION_SYSTEM_PROMPT, VIDEO_ANALYSIS_SYSTEM_PROMPT

logger = structlog.get_logger(__name__)


def _strip_json_fences(raw_text: str) -> str:
    """Remove optional ```json ... ``` markdown wrappers from LLM output."""
    txt = (raw_text or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


class DeepSeekClassifier:
    """Async classifier client communicating with DeepSeek-V3 API (and Gemini API for YouTube video understanding)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        gemini_model: Optional[str] = None,
    ):
        self.api_key = api_key or settings.DEEPSEEK_API_KEY
        self.base_url = base_url or settings.DEEPSEEK_BASE_URL
        self.model = model or settings.DEEPSEEK_MODEL
        self.gemini_api_key = gemini_api_key if gemini_api_key is not None else settings.GEMINI_API_KEY
        self.gemini_model = gemini_model if gemini_model is not None else settings.GEMINI_MODEL

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
        max_retries: int = 1,
    ) -> Dict[str, Any]:
        """Analyze a YouTube video via Google Gemini: fast text-transcript first (~2-3s), or optimized file_uri fallback (thinkingBudget=0, MEDIA_RESOLUTION_LOW)."""
        gemini_key = (self.gemini_api_key or settings.GEMINI_API_KEY or "").strip()

        # Offline mock mode for unit tests
        if gemini_key == "mock-key" or (
            not gemini_key
            and (not self.api_key or self.api_key.startswith("sk-xxxx") or self.api_key == "mock-key")
        ):
            logger.warning("GEMINI_API_KEY not configured (test/mock mode); running heuristic mock video analysis")
            return self._heuristic_mock_analyze_video(video_payload)

        if not gemini_key:
            raise ScraperError(
                "no gemini api key configured",
                "GEMINI_API_KEY is not configured. Add GEMINI_API_KEY to your environment variables to analyze YouTube videos with Google Gemini.",
                status_code=409,
            )

        v_meta = video_payload.get("video") or {}
        video_id = v_meta.get("video_id") or ""
        youtube_url = v_meta.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
        if not youtube_url:
            raise ScraperError("invalid video url", "Missing YouTube video URL for Gemini analysis", status_code=400)

        transcript_text = (video_payload.get("transcript_text") or "").strip()
        has_transcript = bool(video_payload.get("transcript_available") and transcript_text)

        compact_context: Dict[str, Any] = {
            "mode": video_payload.get("mode") or "video",
            "focus": video_payload.get("focus"),
            "video": {
                "video_id": video_id,
                "url": youtube_url,
                "title": v_meta.get("title"),
                "channel_name": v_meta.get("channel_name"),
                "published_at": v_meta.get("published_at"),
                "duration_formatted": v_meta.get("duration_formatted"),
                "view_count": v_meta.get("view_count"),
                "like_count": v_meta.get("like_count"),
                "comment_count": v_meta.get("comment_count"),
            },
        }
        if has_transcript:
            compact_context["transcript_language"] = video_payload.get("transcript_language")
            compact_context["transcript_text"] = transcript_text[:24000]
        if compact_context["mode"] == "reactions":
            compact_context["comment_stats"] = video_payload.get("comment_stats")
            compact_context["comments"] = (video_payload.get("comments") or [])[:50]

        if has_transcript:
            user_prompt_text = (
                "Analyze this YouTube video using the provided timestamped transcript and metadata, then return the compact JSON intelligence report.\n"
                f"Context JSON:\n{json.dumps(compact_context, ensure_ascii=False)}"
            )
            parts_list: List[Dict[str, Any]] = [{"text": user_prompt_text}]
            gen_config: Dict[str, Any] = {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            }
            req_timeout = 30.0
        else:
            user_prompt_text = (
                "Watch and analyze this YouTube video directly, then return the compact JSON intelligence report.\n"
                f"Context JSON:\n{json.dumps(compact_context, ensure_ascii=False)}"
            )
            parts_list = [
                {"file_data": {"file_uri": youtube_url}},
                {"text": user_prompt_text},
            ]
            gen_config = {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            }
            req_timeout = 90.0

        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent"
        req_body = {
            "system_instruction": {
                "parts": [{"text": VIDEO_ANALYSIS_SYSTEM_PROMPT}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": parts_list,
                }
            ],
            "generationConfig": gen_config,
        }

        async with httpx.AsyncClient(timeout=req_timeout) as http_client:
            try:
                resp = await http_client.post(
                    endpoint,
                    params={"key": gemini_key},
                    json=req_body,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    err_detail = resp.text[:400]
                    try:
                        err_json = resp.json()
                        err_detail = (err_json.get("error") or {}).get("message") or err_detail
                    except Exception:
                        pass
                    logger.error(
                        "Gemini video analysis HTTP error",
                        status_code=resp.status_code,
                        mode="transcript" if has_transcript else "file_uri",
                        error=err_detail,
                    )
                    raise ScraperError(
                        "gemini api error",
                        f"Gemini API error ({resp.status_code}): {err_detail}",
                        status_code=resp.status_code if resp.status_code in (400, 401, 403, 429) else 502,
                    )

                data = resp.json() or {}
                candidates = data.get("candidates") or []
                if not candidates:
                    prompt_feedback = data.get("promptFeedback") or {}
                    block_reason = prompt_feedback.get("blockReason") or "No candidates returned by Gemini"
                    raise ScraperError(
                        "gemini blocked or empty",
                        f"Gemini returned no analysis candidates ({block_reason})",
                        status_code=502,
                    )

                parts = ((candidates[0].get("content") or {}).get("parts")) or []
                raw_text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
                clean_json_str = _strip_json_fences(raw_text) or "{}"
                parsed_json = json.loads(clean_json_str)
                if not isinstance(parsed_json, dict):
                    parsed_json = {}
                parsed_json["grounded"] = True
                parsed_json["summary_source"] = "transcript" if has_transcript else "gemini_video"
                return parsed_json

            except ScraperError:
                raise
            except httpx.TimeoutException as t_exc:
                # Never retry timed-out video calls in a loop
                logger.error("Gemini video analysis timed out", error=str(t_exc))
                raise ScraperError(
                    "gemini timeout",
                    f"Gemini video analysis timed out after {int(req_timeout)}s",
                    status_code=504,
                )
            except Exception as exc:
                logger.error("Gemini video analysis failed", error=str(exc))
                raise ScraperError(
                    "gemini api error",
                    f"Gemini video analysis failed: {exc}",
                    status_code=502,
                )

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
