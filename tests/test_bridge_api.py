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


@pytest.mark.asyncio
async def test_bridge_instagram_profile_check_and_scrape(bridge_client, monkeypatch):
    """Verify GET /api/check?target=@username lists last 5 posts and POST /api/scrape with @username targets latest post."""
    from datetime import datetime, timezone
    from src.collectors.instagram import InstagramCollector
    from src.schemas.comment import NormalizedComment

    fake_posts = [
        {
            "post_index": 1,
            "shortcode": "LatestPost111",
            "media_id": "111111",
            "url": "https://www.instagram.com/p/LatestPost111/",
            "thumbnail_url": "https://cdn.instagram.com/thumb1.jpg",
            "caption": "Eng so'nggi yangiliklar: yangi soliq va jarima tartibi e'lon qilindi.",
            "comment_count": 45,
            "like_count": 320,
            "published_at": "2026-10-31 11:00",
        },
        {
            "post_index": 2,
            "shortcode": "SecondPost222",
            "media_id": "222222",
            "url": "https://www.instagram.com/p/SecondPost222/",
            "thumbnail_url": "https://cdn.instagram.com/thumb2.jpg",
            "caption": "Ikkinchi post matni.",
            "comment_count": 18,
            "like_count": 150,
            "published_at": "2026-10-30 19:30",
        },
    ]

    async def mock_fetch_profile_recent_posts(self, username: str, count: int = 5):
        return fake_posts[:count]

    async def mock_fetch_comments(self, target, cursor=None, limit=200):
        yield NormalizedComment(
            platform="instagram",
            external_comment_id=f"ig_c_{target.external_id}_1",
            target_external_id=target.external_id,
            source_name=f"Instagram Post {target.external_id}",
            post_url=target.url,
            comment_url=f"https://www.instagram.com/p/{target.external_id}/c/9001/",
            author_external_id="554433",
            author_username="citizen_uz",
            author_display_name="Azizbek",
            content_text="Bu yangi jarima qonuni xalqni talash va zulm qilish uchundir, boykot!",
            published_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(InstagramCollector, "fetch_profile_recent_posts", mock_fetch_profile_recent_posts)
    monkeypatch.setattr(InstagramCollector, "fetch_comments", mock_fetch_comments)

    # 1. Test GET /api/check?target=@kun.uz
    check_resp = await bridge_client.get("/api/check?target=@kun.uz")
    assert check_resp.status == 200
    check_data = await check_resp.json()
    assert check_data["profile"]["username"] == "@kun.uz"
    assert len(check_data["recent_posts"]) == 2
    assert check_data["recent_posts"][0]["shortcode"] == "LatestPost111"
    assert check_data["recent_posts"][0]["post_index"] == 1

    # 2. Test POST /api/scrape with "@kun.uz" (default post_index=1 -> LatestPost111)
    scrape_resp = await bridge_client.post("/api/scrape", json={"url": "@kun.uz"})
    assert scrape_resp.status == 200
    scrape_data = await scrape_resp.json()
    assert scrape_data["resolved_post"]["shortcode"] == "LatestPost111"
    assert scrape_data["resolved_post"]["post_index"] == 1
    assert "yangi soliq" in scrape_data["resolved_post"]["caption"]
    assert scrape_data["resolved_post"]["published_at"] == "2026-10-31 11:00"
    assert scrape_data["scrape_result"]["newly_ingested"] == 1
    assert scrape_data["stats"]["evaluated"] == 1

    # 3. Test POST /api/scrape with post_index=2 -> SecondPost222 (verify one-off scrape is_active=False)
    scrape_resp2 = await bridge_client.post("/api/scrape", json={"url": "instagram.com/kun.uz", "post_index": 2})
    assert scrape_resp2.status == 200
    scrape_data2 = await scrape_resp2.json()
    assert scrape_data2["resolved_post"]["shortcode"] == "SecondPost222"
    assert scrape_data2["resolved_post"]["post_index"] == 2
    assert scrape_data2["target"]["is_active"] is False

    # 4. Test POST /api/profile (multi-post profile enumeration + comment aggregation)
    prof_resp = await bridge_client.post("/api/profile", json={"handle": "@kun.uz", "posts": 2})
    assert prof_resp.status == 200
    prof_data = await prof_resp.json()
    assert prof_data["profile"]["username"] == "@kun.uz"
    assert prof_data["cadence_and_engagement"]["posts_analyzed"] == 2
    assert len(prof_data["posts"]) == 2
    assert prof_data["comment_stats"]["total_stored"] == 2
    assert prof_data["comment_stats"]["evaluated"] == 2

    # 5. Test GET /api/posts?target=@kun.uz
    posts_resp = await bridge_client.get("/api/posts?target=@kun.uz")
    assert posts_resp.status == 200
    posts_data = await posts_resp.json()
    assert len(posts_data["recent_posts"]) == 2

    # 6. Test POST /api/target/delete
    del_resp = await bridge_client.post("/api/target/delete", json={"target": "SecondPost222", "delete": True})
    assert del_resp.status == 200
    del_data = await del_resp.json()
    assert del_data["action"] == "deleted"


@pytest.mark.asyncio
async def test_bridge_x_409_and_detailed_health(bridge_client):
    """Verify /health/detailed works and scraping X without a session returns 409 instead of a silent no-op."""
    # 1. Test /health/detailed
    h_resp = await bridge_client.get("/health/detailed")
    assert h_resp.status == 200
    h_data = await h_resp.json()
    assert h_data["status"] == "ok"
    assert h_data["platforms_ready"]["instagram"] is True
    assert h_data["platforms_ready"]["x"] is False

    # 2. Test scraping X when no X session or X_BEARER_TOKEN is configured -> must fail loudly with 409
    x_resp = await bridge_client.post(
        "/api/scrape",
        json={"url": "https://x.com/ozodlik/status/2113589268937421273"},
    )
    assert x_resp.status == 409
    x_data = await x_resp.json()
    assert x_data["error"] == "no x session configured"
    assert x_data["platform"] == "x"

    # 3. Add an X session via POST /api/sessions and verify /health/detailed updates
    add_resp = await bridge_client.post(
        "/api/sessions",
        json={
            "platform": "x",
            "account": "x_burner_1",
            "cookies": {"auth_token": "tok123", "ct0": "csrf456"},
        },
    )
    assert add_resp.status == 200
    add_data = await add_resp.json()
    assert add_data["added_session"] == "x_burner_1"
    assert add_data["total_burner_sessions"] == 1

    h_resp2 = await bridge_client.get("/health/detailed")
    h_data2 = await h_resp2.json()
    assert h_data2["platforms_ready"]["x"] is True


@pytest.mark.asyncio
async def test_bridge_x_scrape_replies_and_explicit_errors(bridge_client, monkeypatch):
    """Verify X scraper fetches replies, runs DeepSeek evaluation, and returns explicit errors (401/404/429) while incrementing error_count."""
    from src.collectors.twitter_x import TwitterXCollector
    from src.llm.processor import MicroBatchProcessor

    # 1. Seed X burner session
    await bridge_client.post(
        "/api/sessions",
        json={
            "platform": "x",
            "account": "x_burner_test",
            "cookies": {"auth_token": "valid_auth", "ct0": "valid_ct0"},
        },
    )

    async def mock_refresh_ct0(self, cookies, user_agent, proxy_url=None):
        return cookies

    monkeypatch.setattr(TwitterXCollector, "_refresh_ct0_cookie", mock_refresh_ct0)

    # Mock twikit response for a tweet with 2 replies (1 long hostile reply, 1 short emoji noise reply)
    async def mock_fetch_via_twikit(self, tweet_id, target, cookies, user_agent, proxy_url, cursor, limit):
        if tweet_id == "2111136681747452363":
            sample_gql = {
                "data": {
                    "threaded_conversation_with_injections_v2": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": f"tweet-{tweet_id}",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": tweet_id,
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "rest_id": "u_author",
                                                                    "legacy": {
                                                                        "screen_name": "AsliddinKamalov",
                                                                        "name": "Asliddin Kamalov",
                                                                    },
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "id_str": tweet_id,
                                                            "full_text": "Focal tweet text for testing X replies.",
                                                            "created_at": "Wed Oct 28 12:00:00 +0000 2026",
                                                            "favorite_count": 42,
                                                            "reply_count": 2,
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "conversationthread-101",
                                        "content": {
                                            "items": [
                                                {
                                                    "item": {
                                                        "itemContent": {
                                                            "tweet_results": {
                                                                "result": {
                                                                    "__typename": "Tweet",
                                                                    "rest_id": "999001",
                                                                    "core": {
                                                                        "user_results": {
                                                                            "result": {
                                                                                "rest_id": "u_replier_1",
                                                                                "legacy": {
                                                                                    "screen_name": "angry_critic",
                                                                                    "name": "Angry Critic",
                                                                                },
                                                                            }
                                                                        }
                                                                    },
                                                                    "legacy": {
                                                                        "id_str": "999001",
                                                                        "full_text": "All these officials are corrupt thieves stealing public money every single day!",
                                                                        "created_at": "Wed Oct 28 12:05:00 +0000 2026",
                                                                        "favorite_count": 5,
                                                                        "reply_count": 0,
                                                                    },
                                                                }
                                                            }
                                                        }
                                                    }
                                                },
                                                {
                                                    "item": {
                                                        "itemContent": {
                                                            "tweet_results": {
                                                                "result": {
                                                                    "__typename": "Tweet",
                                                                    "rest_id": "999002",
                                                                    "core": {
                                                                        "user_results": {
                                                                            "result": {
                                                                                "rest_id": "u_replier_2",
                                                                                "legacy": {
                                                                                    "screen_name": "emoji_fan",
                                                                                    "name": "Emoji Fan",
                                                                                },
                                                                            }
                                                                        }
                                                                    },
                                                                    "legacy": {
                                                                        "id_str": "999002",
                                                                        "full_text": "🔥🔥🔥",
                                                                        "created_at": "Wed Oct 28 12:06:00 +0000 2026",
                                                                        "favorite_count": 1,
                                                                        "reply_count": 0,
                                                                    },
                                                                }
                                                            }
                                                        }
                                                    }
                                                },
                                            ]
                                        },
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
            focal, replies, _ = self._parse_tweet_detail_payload(sample_gql, tweet_id, target)
            return focal, replies, cookies

        if tweet_id == "2113589268937421273":
            # Tweet that genuinely has 0 replies
            zero_gql = {
                "data": {
                    "threaded_conversation_with_injections_v2": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": f"tweet-{tweet_id}",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": tweet_id,
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "rest_id": "u_ozodlik",
                                                                    "legacy": {"screen_name": "ozodlik", "name": "Ozodlik"},
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "id_str": tweet_id,
                                                            "full_text": "Post with zero replies.",
                                                            "created_at": "Wed Oct 28 13:00:00 +0000 2026",
                                                            "favorite_count": 10,
                                                            "reply_count": 0,
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
            focal, replies, _ = self._parse_tweet_detail_payload(zero_gql, tweet_id, target)
            return focal, replies, cookies

        if tweet_id == "9999999999999999999":
            from src.collectors.base import ScraperError
            raise ScraperError("tweet not found", f"Tweet {tweet_id} not found", status_code=404)

        if tweet_id == "8888888888888888888":
            from src.collectors.base import ScraperError
            raise ScraperError("x session rejected", "X session rejected or cookies expired", status_code=401)

        return False, [], cookies

    monkeypatch.setattr(TwitterXCollector, "_fetch_via_twikit", mock_fetch_via_twikit)

    # Mock LLM evaluation for the hostile reply
    from src.llm.client import DeepSeekClassifier
    from src.schemas.llm import CommentVerdict

    async def mock_evaluate_batch(self, items, source_context="Public Social Media Post"):
        return (
            [
                CommentVerdict(
                    id=item.id,
                    on_topic=True,
                    severely_negative=True,
                    severity_score=8,
                    reason="Direct corruption accusation against officials.",
                )
                for item in items
            ],
            {},
        )

    monkeypatch.setattr(DeepSeekClassifier, "evaluate_batch", mock_evaluate_batch)

    # 2. Test X post with replies -> newly_ingested=2, noise_skipped=1, evaluated=1, flagged_count=1
    resp1 = await bridge_client.post(
        "/api/scrape",
        json={"url": "https://x.com/AsliddinKamalov/status/2111136681747452363"},
    )
    assert resp1.status == 200
    data1 = await resp1.json()
    assert data1["scrape_result"]["status"] == "completed"
    assert data1["scrape_result"]["newly_ingested"] == 2
    assert data1["stats"]["total_comments"] == 2
    assert data1["stats"]["noise_skipped"] == 1
    assert data1["stats"]["evaluated"] == 1
    assert data1["stats"]["flagged_count"] == 1
    assert data1["tweet"]["author"] == "@AsliddinKamalov"
    assert len(data1["flagged_comments"]) == 1
    assert data1["flagged_comments"][0]["score"] == 8

    # 3. Test X post with genuinely 0 replies -> status="completed", total_comments=0
    resp2 = await bridge_client.post(
        "/api/scrape",
        json={"url": "https://x.com/ozodlik/status/2113589268937421273"},
    )
    assert resp2.status == 200
    data2 = await resp2.json()
    assert data2["scrape_result"]["status"] == "completed"
    assert data2["stats"]["total_comments"] == 0
    assert data2["tweet"]["author"] == "@ozodlik"

    # 4. Test non-existent/deleted tweet -> HTTP 404 {"error": "tweet not found"}
    resp3 = await bridge_client.post(
        "/api/scrape",
        json={"url": "https://x.com/someone/status/9999999999999999999"},
    )
    assert resp3.status == 404
    data3 = await resp3.json()
    assert data3["error"] == "tweet not found"

    # 5. Test rejected X session -> HTTP 401 {"error": "x session rejected"} and error_count incremented
    resp4 = await bridge_client.post(
        "/api/scrape",
        json={"url": "https://x.com/someone/status/8888888888888888888"},
    )
    assert resp4.status == 401
    data4 = await resp4.json()
    assert data4["error"] == "x session rejected"

    sess_resp = await bridge_client.get("/api/sessions?platform=x")
    sess_data = await sess_resp.json()
    assert sess_data["sessions"][0]["error_count"] == 1

