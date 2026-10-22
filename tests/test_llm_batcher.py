"""Unit tests for the DeepSeek LLM pipeline, micro-batching, and prompt cache preservation."""

import pytest
from src.llm.prompts import CLASSIFICATION_SYSTEM_PROMPT
from src.llm.processor import is_trivial_comment, MicroBatchProcessor
from src.llm.client import DeepSeekClassifier
from src.schemas.llm import CommentBatchItem


def test_system_prompt_prefix_cache_invariants():
    """Ensure system prompt is completely static and contains no dynamic format templates."""
    forbidden_placeholders = ["{chat_id}", "{username}", "{timestamp}", "{url}", "{comments}", "{date}", "{name}"]
    for placeholder in forbidden_placeholders:
        assert placeholder not in CLASSIFICATION_SYSTEM_PROMPT
    assert len(CLASSIFICATION_SYSTEM_PROMPT.split()) > 300  # Detailed and >1024 tokens
    assert "Azerbaijani" in CLASSIFICATION_SYSTEM_PROMPT
    assert "Russian" in CLASSIFICATION_SYSTEM_PROMPT
    assert "Turkish" in CLASSIFICATION_SYSTEM_PROMPT
    assert "NOISE REDUCTION RULES" in CLASSIFICATION_SYSTEM_PROMPT


def test_is_trivial_comment():
    assert is_trivial_comment("") is True
    assert is_trivial_comment("  ") is True
    assert is_trivial_comment("👍") is True
    assert is_trivial_comment("😂😂🔥") is True
    assert is_trivial_comment("ok") is True
    assert is_trivial_comment("@user") is True
    assert is_trivial_comment("https://example.com") is True
    
    # Substantive comments must NOT be trivial
    assert is_trivial_comment("Bu qanun tamamilə haqsızdır!") is False
    assert is_trivial_comment("These new tax rules are completely absurd and oppressive.") is False


@pytest.mark.asyncio
async def test_deepseek_mock_evaluation():
    classifier = DeepSeekClassifier(api_key="mock-key")
    items = [
        CommentBatchItem(id="1", text="Bugün hava çox qəşəngdir, parka gedirəm"),
        CommentBatchItem(id="2", text="Bu yeni vergi qaydaları soyğundur, rejim camaatı boğur etiraz edək!"),
        CommentBatchItem(id="3", text="Just regular chat without any politics"),
    ]
    verdicts, meta = await classifier.evaluate_batch(items)
    assert len(verdicts) == 3
    assert verdicts[0].severely_negative is False
    assert verdicts[1].severely_negative is True
    assert verdicts[1].severity_score >= 7
    assert verdicts[2].severely_negative is False
