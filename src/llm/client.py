"""DeepSeek API Async Client with prefix caching optimization and resilience."""

import json
import asyncio
from typing import List, Optional, Tuple, Dict, Any
from openai import AsyncOpenAI
import structlog
from src.config import settings
from src.schemas.llm import CommentBatchItem, CommentVerdict, BatchEvaluationResponse
from src.llm.prompts import CLASSIFICATION_SYSTEM_PROMPT

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
