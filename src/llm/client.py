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


def _parse_duration_seconds(duration_formatted: Any, duration_seconds: Any = 0) -> int:
    """Parse 'HH:MM:SS' or 'MM:SS' into total seconds."""
    try:
        if duration_seconds and int(duration_seconds) > 0:
            return int(duration_seconds)
    except (TypeError, ValueError):
        pass
    raw = str(duration_formatted or "").strip()
    if not raw:
        return 0
    parts = [p.strip() for p in raw.split(":")]
    if not all(p.isdigit() for p in parts):
        return 0
    nums = [int(p) for p in parts]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0


def _format_seconds_ts(sec: int) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    sec = max(0, int(sec))
    if sec >= 3600:
        return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _offset_timestamp_str(raw_ts: Any, start_offset: int, window_size: int = 900) -> str:
    """Offset a relative chunk timestamp by start_offset seconds if needed."""
    text = str(raw_ts or "").strip()
    if not text or start_offset <= 0:
        return text
    m = re.match(r"^(\[?)(\d{1,2}(?::\d{2}){1,2})(\]?)(.*)$", text)
    if not m:
        return text
    prefix, ts_part, suffix, rest = m.groups()
    rel_sec = _parse_duration_seconds(ts_part, 0)
    # If Gemini returned a clip-relative timestamp (within [0, window_size + 5]), shift by start_offset
    if rel_sec < start_offset and rel_sec <= window_size + 5:
        abs_sec = rel_sec + start_offset
    else:
        abs_sec = rel_sec
    return f"{prefix}{_format_seconds_ts(abs_sec)}{suffix}{rest}"


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

    async def _post_gemini_json(
        self,
        endpoint: str,
        gemini_key: str,
        req_body: Dict[str, Any],
        req_timeout: float,
        mode_label: str,
    ) -> Dict[str, Any]:
        """Send a single Gemini generateContent request.

        - Never retries 504 timeouts.
        - Only retries upstream 503/429 errors, max 2 retries with exponential backoff (2s, 5s).
        """
        backoffs = [2.0, 5.0]
        max_attempts = 1 + len(backoffs)

        async with httpx.AsyncClient(timeout=req_timeout) as http_client:
            for attempt in range(1, max_attempts + 1):
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
                            mode=mode_label,
                            attempt=attempt,
                            error=err_detail,
                        )
                        if resp.status_code in (429, 503) and attempt < max_attempts:
                            await asyncio.sleep(backoffs[attempt - 1])
                            continue
                        raise ScraperError(
                            "gemini api error",
                            f"Gemini API error ({resp.status_code}): {err_detail}",
                            status_code=resp.status_code if resp.status_code in (400, 401, 403, 429, 503, 504) else 502,
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
                    return parsed_json if isinstance(parsed_json, dict) else {}

                except ScraperError:
                    raise
                except httpx.TimeoutException as t_exc:
                    # Do NOT retry 504 timeouts inside the client
                    logger.error("Gemini video analysis timed out", mode=mode_label, timeout=req_timeout, error=str(t_exc))
                    raise ScraperError(
                        "gemini timeout",
                        f"Gemini video analysis timed out after {int(req_timeout)}s ({mode_label})",
                        status_code=504,
                    )
                except Exception as exc:
                    logger.error("Gemini video analysis failed", mode=mode_label, error=str(exc))
                    raise ScraperError(
                        "gemini api error",
                        f"Gemini video analysis failed: {exc}",
                        status_code=502,
                    )

        raise ScraperError("gemini api error", "Gemini video analysis exhausted retries", status_code=502)

    async def analyze_video(
        self,
        video_payload: Dict[str, Any],
        max_retries: int = 1,
    ) -> Dict[str, Any]:
        """Analyze a YouTube video via Google Gemini using duration-tiered scaling and parallel chunking."""
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
        dur = _parse_duration_seconds(v_meta.get("duration_formatted"), v_meta.get("duration_seconds"))

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

        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent"

        # Fast Transcript Path (~2-3s)
        if has_transcript:
            user_prompt_text = (
                "Analyze this YouTube video using the provided timestamped transcript and metadata, then return the compact JSON intelligence report.\n"
                f"Context JSON:\n{json.dumps(compact_context, ensure_ascii=False)}"
            )
            req_body = {
                "system_instruction": {"parts": [{"text": VIDEO_ANALYSIS_SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt_text}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
            parsed_json = await self._post_gemini_json(
                endpoint=endpoint,
                gemini_key=gemini_key,
                req_body=req_body,
                req_timeout=45.0,
                mode_label="transcript",
            )
            parsed_json["grounded"] = True
            parsed_json["summary_source"] = "transcript"
            return parsed_json

        # Tier 3: dur > 3600s (> 60 min) -> Split into 15-minute (900s) windows in parallel (max concurrency 4)
        if dur > 3600:
            window_size = 900
            windows = [(start, min(start + window_size, dur)) for start in range(0, dur, window_size)]
            sem = asyncio.Semaphore(4)

            async def _analyze_chunk(idx: int, start_s: int, end_s: int) -> Optional[Dict[str, Any]]:
                async with sem:
                    chunk_ctx = dict(compact_context)
                    chunk_ctx["chunk_index"] = idx + 1
                    chunk_ctx["chunk_total"] = len(windows)
                    chunk_ctx["chunk_window"] = f"{_format_seconds_ts(start_s)} to {_format_seconds_ts(end_s)}"
                    chunk_prompt = (
                        f"Watch segment {_format_seconds_ts(start_s)} to {_format_seconds_ts(end_s)} of this YouTube video "
                        f"and return the compact JSON intelligence report for this window.\n"
                        f"Context JSON:\n{json.dumps(chunk_ctx, ensure_ascii=False)}"
                    )
                    chunk_body = {
                        "system_instruction": {"parts": [{"text": VIDEO_ANALYSIS_SYSTEM_PROMPT}]},
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "file_data": {
                                            "file_uri": youtube_url,
                                            "mime_type": "video/*",
                                        },
                                        "video_metadata": {
                                            "start_offset": f"{start_s}s",
                                            "end_offset": f"{end_s}s",
                                            "fps": 0.5,
                                        },
                                    },
                                    {"text": chunk_prompt},
                                ],
                            }
                        ],
                        "generationConfig": {
                            "temperature": 0.1,
                            "responseMimeType": "application/json",
                            "thinkingConfig": {"thinkingBudget": 0},
                            "mediaResolution": "MEDIA_RESOLUTION_LOW",
                        },
                    }
                    res = await self._post_gemini_json(
                        endpoint=endpoint,
                        gemini_key=gemini_key,
                        req_body=chunk_body,
                        req_timeout=180.0,
                        mode_label=f"tier3_chunk_{idx + 1}",
                    )
                    # Offset timestamps in key_moments, claims_and_allegations, and reactions_by_video_part by start_s
                    for km in res.get("key_moments") or []:
                        if isinstance(km, dict) and km.get("timestamp"):
                            km["timestamp"] = _offset_timestamp_str(km["timestamp"], start_s, window_size)
                    for cl in res.get("claims_and_allegations") or []:
                        if isinstance(cl, dict) and cl.get("timestamp"):
                            cl["timestamp"] = _offset_timestamp_str(cl["timestamp"], start_s, window_size)
                    aud = res.get("audience_reaction")
                    if isinstance(aud, dict):
                        for rp in aud.get("reactions_by_video_part") or []:
                            if isinstance(rp, dict) and rp.get("video_part"):
                                rp["video_part"] = _offset_timestamp_str(rp["video_part"], start_s, window_size)
                    return res

            chunk_tasks = [_analyze_chunk(i, s, e) for i, (s, e) in enumerate(windows)]
            chunk_results_raw = await asyncio.gather(*chunk_tasks, return_exceptions=True)
            valid_chunks: List[Dict[str, Any]] = [r for r in chunk_results_raw if isinstance(r, dict) and r]
            if not valid_chunks:
                first_err = next((r for r in chunk_results_raw if isinstance(r, Exception)), None)
                if isinstance(first_err, ScraperError):
                    raise first_err
                raise ScraperError("gemini chunking failed", f"All Tier 3 video chunks failed: {first_err}", status_code=504)

            if len(valid_chunks) == 1:
                merged = valid_chunks[0]
            else:
                # Fast text-merge call across chunk reports
                merge_input = {
                    "mode": compact_context["mode"],
                    "focus": compact_context["focus"],
                    "video": compact_context["video"],
                    "chunk_reports": valid_chunks,
                }
                if compact_context["mode"] == "reactions":
                    merge_input["comment_stats"] = compact_context.get("comment_stats")
                    merge_input["comments"] = compact_context.get("comments")
                merge_prompt = (
                    "Synthesize these chronological 15-minute video chunk reports into a single unified, compact JSON intelligence report "
                    "(preserving exact offset timestamps in key_moments and claims_and_allegations).\n"
                    f"Input JSON:\n{json.dumps(merge_input, ensure_ascii=False)}"
                )
                merge_body = {
                    "system_instruction": {"parts": [{"text": VIDEO_ANALYSIS_SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": merge_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                        "thinkingConfig": {"thinkingBudget": 0},
                    },
                }
                try:
                    merged = await self._post_gemini_json(
                        endpoint=endpoint,
                        gemini_key=gemini_key,
                        req_body=merge_body,
                        req_timeout=60.0,
                        mode_label="tier3_merge",
                    )
                except Exception as m_err:
                    logger.warning("Tier 3 text-merge fallback to deterministic combine", error=str(m_err))
                    max_sev = max(int(c.get("severity_score") or 0) for c in valid_chunks)
                    merged = {
                        "summary": " ".join(str(c.get("summary") or "") for c in valid_chunks if c.get("summary"))[:800],
                        "language": valid_chunks[0].get("language") or "unknown",
                        "on_topic": any(bool(c.get("on_topic")) for c in valid_chunks),
                        "severity_score": max_sev,
                        "severely_negative": max_sev >= 7,
                        "tone_and_stance": valid_chunks[0].get("tone_and_stance") or "Mixed commentary",
                        "main_topics": list(dict.fromkeys(t for c in valid_chunks for t in (c.get("main_topics") or [])))[:6],
                        "key_moments": [km for c in valid_chunks for km in (c.get("key_moments") or [])][:5],
                        "claims_and_allegations": [cl for c in valid_chunks for cl in (c.get("claims_and_allegations") or [])][:5],
                        "risk_assessment": valid_chunks[0].get("risk_assessment") or "",
                        "focused_answer": next((c.get("focused_answer") for c in valid_chunks if c.get("focused_answer")), None),
                        "audience_reaction": next((c.get("audience_reaction") for c in valid_chunks if c.get("audience_reaction")), None),
                    }

            merged["grounded"] = True
            merged["summary_source"] = "gemini_video"
            return merged

        # Tier 1 (dur <= 1200s / <= 20m) vs Tier 2 (1200s < dur <= 3600s / 20-60m)
        user_prompt_text = (
            "Watch and analyze this YouTube video directly, then return the compact JSON intelligence report.\n"
            f"Context JSON:\n{json.dumps(compact_context, ensure_ascii=False)}"
        )

        if dur > 1200:
            # Tier 2 (20-60 min): low resolution + 1.0 fps + mime_type="video/*" + 420s timeout
            video_part: Dict[str, Any] = {
                "file_data": {
                    "file_uri": youtube_url,
                    "mime_type": "video/*",
                },
                "video_metadata": {
                    "fps": 1.0,
                },
            }
            gen_config = {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            }
            req_timeout = 420.0
            mode_label = "tier2_file_uri_low"
        else:
            # Tier 1 (<= 20 min): full video, default resolution, scaled timeout [120s..300s]
            video_part = {
                "file_data": {
                    "file_uri": youtube_url,
                    "mime_type": "video/*",
                }
            }
            gen_config = {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            }
            req_timeout = min(300.0, max(120.0, 45.0 + dur * 0.15))
            mode_label = "tier1_file_uri"

        req_body = {
            "system_instruction": {"parts": [{"text": VIDEO_ANALYSIS_SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        video_part,
                        {"text": user_prompt_text},
                    ],
                }
            ],
            "generationConfig": gen_config,
        }

        parsed_json = await self._post_gemini_json(
            endpoint=endpoint,
            gemini_key=gemini_key,
            req_body=req_body,
            req_timeout=req_timeout,
            mode_label=mode_label,
        )
        parsed_json["grounded"] = True
        parsed_json["summary_source"] = "gemini_video"
        return parsed_json

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
