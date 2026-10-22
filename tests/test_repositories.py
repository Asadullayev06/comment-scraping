"""Unit tests for SQLite/PostgreSQL async repositories and deduplication."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.db.base import Base
from src.db.repositories.target_repo import TargetRepository
from src.db.repositories.author_repo import AuthorRepository
from src.db.repositories.comment_repo import CommentRepository
from src.db.repositories.session_repo import SessionRepository


@pytest.fixture
async def async_session() -> AsyncSession:
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_target_crud(async_session: AsyncSession):
    repo = TargetRepository(async_session)
    target = await repo.create_or_activate(
        platform="telegram",
        target_type="telegram_chat",
        external_id="-100123456789",
        url="https://t.me/testgroup",
        title_or_name="Test Group",
    )
    assert target.id is not None
    assert target.is_active is True

    # Re-activating should return existing
    target2 = await repo.create_or_activate(
        platform="telegram",
        target_type="telegram_chat",
        external_id="-100123456789",
    )
    assert target.id == target2.id

    # Pause
    await repo.set_active(target.id, False)
    t = await repo.get_by_id(target.id)
    assert t.is_active is False


@pytest.mark.asyncio
async def test_author_upsert(async_session: AsyncSession):
    repo = AuthorRepository(async_session)
    author = await repo.upsert_author(
        platform="telegram",
        external_user_id="99887766",
        username="@JohnDoe",
        display_name="John Doe",
    )
    assert author.username == "johndoe"  # normalized lowercase without @

    # Update author display name
    author2 = await repo.upsert_author(
        platform="telegram",
        external_user_id="99887766",
        display_name="John D.",
    )
    assert author.id == author2.id
    assert author2.display_name == "John D."


@pytest.mark.asyncio
async def test_comment_deduplication(async_session: AsyncSession):
    repo = CommentRepository(async_session)
    c1 = await repo.insert_single(
        platform="telegram",
        external_comment_id="tg_100_1",
        content_text="First message",
    )
    assert c1 is not None

    # Duplicate insert with same (platform, external_comment_id) should be ignored
    c2 = await repo.insert_single(
        platform="telegram",
        external_comment_id="tg_100_1",
        content_text="Duplicate message attempt",
    )
    assert c2 is None


@pytest.mark.asyncio
async def test_session_encryption(async_session: AsyncSession):
    repo = SessionRepository(async_session, encryption_key="secret-key-123")
    data = {"sessionid": "abc123xyz", "csrftoken": "tok456"}
    encrypted = repo.encrypt_data(data)
    assert encrypted != str(data)

    decrypted = repo.decrypt_data(encrypted)
    assert decrypted["sessionid"] == "abc123xyz"
    assert decrypted["csrftoken"] == "tok456"
