"""Unit tests for the internal Hermes Bridge HTTP API (src/api/bridge.py)."""

import json
import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

import src.api.bridge as bridge_mod
from src.db.base import Base
from src.db.repositories.target_repo import TargetRepository
from src.db.repositories.author_repo import AuthorRepository
from src.db.repositories.comment_repo import CommentRepository
from src.db.repositories.session_repo import SessionRepository


@pytest.fixture
async def bridge_client(monkeypatch):
    """Create an in-memory SQLite database and an aiohttp TestClient for the bridge API."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(bridge_mod, "async_session_factory", session_factory)

    # Seed sample target, author, comments, and burner session
    async with session_factory() as session:
        t_repo = TargetRepository(session)
        a_repo = AuthorRepository(session)
        c_repo = CommentRepository(session)
        s_repo = SessionRepository(session)

        target = await t_repo.create_or_activate(
            platform="instagram",
            target_type="post_url",
            external_id="DewaVyCEfCH",
            url="https://www.instagram.com/p/DewaVyCEfCH/",
            title_or_name="Instagram Post DewaVyCEfCH",
        )
        author = await a_repo.upsert_author(
            platform="instagram",
            external_user_id="998877",
            username="angry_citizen",
            display_name="Angry Citizen",
        )

        # Flagged hostile comment
        c1 = await c_repo.insert_single(
            platform="instagram",
            external_comment_id="ig_101",
            content_text="Bu yangi qonun xalqni talash uchun chiqarilgan, hamma norozilikka chiqsin!",
            target_id=target.id,
            author_id=author.id,
            source_name="Instagram Post",
            comment_url="https://www.instagram.com/p/DewaVyCEfCH/c/101/",
        )
        await c_repo.update_llm_verdict(
            comment_id=c1.id,
            is_on_topic=True,
            is_severely_negative=True,
            severity_score=9,
            llm_reason="Accuses state law of robbery and incites public protest.",
        )

        # Normal neutral comment
        c2 = await c_repo.insert_single(
            platform="instagram",
            external_comment_id="ig_102",
            content_text="Bugun ob-havo juda yaxshi ekan, hammaga omad tilayman.",
            target_id=target.id,
            author_id=author.id,
            source_name="Instagram Post",
            comment_url="https://www.instagram.com/p/DewaVyCEfCH/c/102/",
        )
        await c_repo.update_llm_verdict(
            comment_id=c2.id,
            is_on_topic=False,
            is_severely_negative=False,
            severity_score=1,
            llm_reason=None,
        )

        # Failed evaluation comment (score=None, status=failed) with pipe-only author name
        pipe_author = await a_repo.upsert_author(
            platform="telegram",
            external_user_id="8196222540",
            username=None,
            display_name="||||| None",
        )
        c3 = await c_repo.insert_single(
            platform="telegram",
            external_comment_id="tg_103",
            content_text="Ba'zi yangiliklar haqida batafsil fikr yuritish kerak.",
            target_id=target.id,
            author_id=pipe_author.id,
            source_name="Telegram Group",
        )
        await c_repo.update_llm_verdict(
            comment_id=c3.id,
            is_on_topic=None,
            is_severely_negative=False,
            severity_score=None,
            llm_reason="Evaluation failed: transient network timeout",
            llm_status="failed",
        )

        await s_repo.add_session(
            platform="instagram",
            account_identifier="burner_ig_1",
            cookies={"sessionid": "12345%3Aabcdef"},
        )
        await session.commit()

    app = bridge_mod.create_bridge_app(mtproto=None)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_bridge_digest(bridge_client):
    resp = await bridge_client.get("/api/digest?hours=24&min_score=7")
    assert resp.status == 200
    data = await resp.json()
    assert data["total_ingested"] == 3
    assert data["failed"] == 1
    assert data["flagged_count"] == 1
    assert len(data["flagged_comments"]) == 1
    assert data["flagged_comments"][0]["score"] == 9
    assert len(json.dumps(data)) < 3000


@pytest.mark.asyncio
async def test_bridge_check_target(bridge_client):
    resp = await bridge_client.get("/api/check?target=DewaVyCEfCH")
    assert resp.status == 200
    data = await resp.json()
    assert data["target"]["external_id"] == "DewaVyCEfCH"
    assert data["stats"]["flagged_count"] >= 1
    assert len(data["flagged_comments"]) >= 1
    assert len(data["recent_sample"]) >= 1
    assert len(json.dumps(data)) < 3000


@pytest.mark.asyncio
async def test_bridge_search(bridge_client):
    resp = await bridge_client.get("/api/search?q=norozilikka")
    assert resp.status == 200
    data = await resp.json()
    assert data["total_matches"] == 1
    assert data["results"][0]["score"] == 9
    assert data["results"][0]["occurrences"] == 1

    # Search by failed status / reason and verify pipe-only display_name is cleaned
    resp_fail = await bridge_client.get("/api/search?failed=true")
    assert resp_fail.status == 200
    fail_data = await resp_fail.json()
    assert fail_data["total_matches"] == 1
    assert fail_data["results"][0]["score"] is None
    assert fail_data["results"][0]["status"] == "failed"
    assert fail_data["results"][0]["author"] == "User [id:8196222540]"
    assert fail_data["results"][0]["occurrences"] == 1


@pytest.mark.asyncio
async def test_bridge_sessions(bridge_client):
    resp = await bridge_client.get("/api/sessions?reset=true")
    assert resp.status == 200
    data = await resp.json()
    assert data["total_burner_sessions"] == 1
    assert data["sessions"][0]["account"] == "burner_ig_1"
    assert data["sessions"][0]["status"] == "active"
