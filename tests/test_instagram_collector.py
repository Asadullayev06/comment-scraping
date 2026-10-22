"""Unit tests for the instagrapi-backed Instagram collector and challenge handling."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.db.base import Base
from src.db.models import MonitoredTarget, ScraperSession
from src.schemas.comment import NormalizedComment
from src.collectors.instagram import InstagramCollector, build_instagram_comment_url
from src.collectors.pool_manager import SessionPoolManager


@pytest.fixture
async def async_session() -> AsyncSession:
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session

    await test_engine.dispose()


def test_build_instagram_comment_url():
    url = build_instagram_comment_url("C7XyZ12345", "17999888777")
    assert url == "https://www.instagram.com/p/C7XyZ12345/c/17999888777/"


def test_normalized_comment_child_count():
    c = NormalizedComment(
        platform="instagram",
        external_comment_id="ig_123456",
        child_comment_count=14,
        author_external_id="998877",
        content_text="This is a top-level comment with replies",
    )
    assert c.child_comment_count == 14
    assert c.parent_external_id is None


@pytest.mark.asyncio
async def test_pool_manager_challenge_reporting(async_session: AsyncSession):
    pool_mgr = SessionPoolManager(async_session)

    # Add a burner session
    session = await pool_mgr.session_repo.add_session(
        platform="instagram",
        account_identifier="burner_test_1",
        cookies={"sessionid": "mock_session_id"},
    )
    await async_session.commit()

    assert session.status == "active"
    assert session.cooldown_until is None

    # Report challenge
    await pool_mgr.report_challenge(session.id, error_type="ChallengeRequired", cooldown_hours=6)

    updated = await pool_mgr.session_repo.session.get(ScraperSession, session.id)
    assert updated.status == "challenged"
    assert updated.cooldown_until is not None
    assert updated.error_count == 1
