"""Unit tests for Facebook Collector parsing, profile extraction, and comment normalization."""

import pytest
from datetime import datetime, timezone
from src.collectors.facebook import (
    FacebookCollector,
    parse_count_str,
    extract_feedback_id_and_tokens,
    parse_profile_meta_from_html,
    extract_comments_from_json,
    extract_posts_from_json,
)
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget


def test_parse_count_str():
    assert parse_count_str("119M") == 119_000_000
    assert parse_count_str("45.2K") == 45_200
    assert parse_count_str("1,250") == 1250
    assert parse_count_str("500") == 500
    assert parse_count_str(100) == 100
    assert parse_count_str(None) is None
    assert parse_count_str("invalid") is None


def test_extract_feedback_id_and_tokens():
    sample_html = """
    <html>
    <head>
    <script>
    ["DTSGInitialData",[],{"token":"AQHR_test_dtsg_token:123"}]
    </script>
    <script>
    var x = {"feedback_id":"10114294029482931","jazoest":"2954"};
    </script>
    </head>
    </html>
    """
    fbid, dtsg, jazoest = extract_feedback_id_and_tokens(sample_html)
    assert fbid == "10114294029482931"
    assert dtsg == "AQHR_test_dtsg_token:123"
    assert jazoest == "2954"


def test_parse_profile_meta_from_html():
    sample_html = """
    <html>
    <head>
    <meta property="og:title" content="Mark Zuckerberg - Home | Facebook">
    <meta property="og:description" content="Mark Zuckerberg. 119M likes · 119M followers. Founder and CEO at Meta. Building AI and the metaverse.">
    <meta property="og:image" content="https://scontent.xx.fbcdn.net/v/avatar.jpg?stp=cp0&amp;oh=123">
    <script>{"user": {"is_verified": true}}</script>
    </head>
    </html>
    """
    meta = parse_profile_meta_from_html(sample_html, "zuck")
    assert meta["platform"] == "facebook"
    assert meta["username"] == "@zuck"
    assert meta["full_name"] == "Mark Zuckerberg"
    assert "Founder and CEO at Meta" in meta["bio"]
    assert meta["followers"] == 119_000_000
    assert meta["is_verified"] is True
    assert "https://scontent.xx.fbcdn.net/v/avatar.jpg" in meta["avatar_url"]


def test_parse_profile_meta_login_wall():
    """Verify og:title == 'Facebook' with login description is treated as login wall with no fake metadata."""
    sample_html = """
    <html>
    <head>
    <meta property="og:title" content="Facebook">
    <meta property="og:description" content="Log into Facebook to start sharing and connecting with your friends, family, and people you know.">
    <script>{"is_verified": true}</script>
    </head>
    </html>
    """
    meta = parse_profile_meta_from_html(sample_html, "61592582919537")
    assert meta["is_login_wall"] is True
    assert meta["full_name"] is None
    assert meta["bio"] is None
    assert meta["followers"] is None
    assert meta["is_verified"] is False


@pytest.mark.asyncio
async def test_facebook_collector_login_wall_raises(monkeypatch):
    """Verify fetch_profile_info_and_posts raises ScraperError(facebook profile requires login) when encountering shell."""
    from src.collectors.base import ScraperError

    collector = FacebookCollector()
    sample_html = """
    <html>
    <head>
    <meta property="og:title" content="Facebook">
    <meta property="og:description" content="">
    </head>
    </html>
    """

    class FakeResponse:
        status_code = 200
        text = sample_html

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    with pytest.raises(ScraperError) as exc_info:
        await collector.fetch_profile_info_and_posts("61592582919537")
    assert exc_info.value.error_code == "facebook profile requires login"
    assert exc_info.value.status_code == 401


def test_extract_comments_from_json():
    sample_json = {
        "feedback": {
            "comments": {
                "edges": [
                    {
                        "node": {
                            "id": "Y29tbWVudDoxMTAx",
                            "legacy_fbid": "1101",
                            "body": {"text": "Ajoyib yangilik, tabriklaymiz!"},
                            "author": {
                                "id": "10008899",
                                "name": "Alisher Navoiy",
                                "url": "https://www.facebook.com/10008899",
                            },
                            "created_time": 1714567890,
                        }
                    },
                    {
                        "node": {
                            "id": "Y29tbWVudDoxMTAy",
                            "legacy_fbid": "1102",
                            "body": {"text": "Bu qaror noto'g'ri bo'ldi, qayta ko'rib chiqing."},
                            "author": {
                                "id": "10007788",
                                "name": "Bobur Mirzo",
                            },
                            "created_time": 1714567950,
                        }
                    },
                ]
            }
        }
    }
    comments = extract_comments_from_json(
        sample_json,
        post_id="post_999",
        source_name="Facebook Target",
        post_url="https://www.facebook.com/post_999",
    )
    assert len(comments) == 2
    c1, c2 = comments[0], comments[1]
    assert c1.platform == "facebook"
    assert c1.external_comment_id == "fb_1101"
    assert c1.content_text == "Ajoyib yangilik, tabriklaymiz!"
    assert c1.author_display_name == "Alisher Navoiy"
    assert c1.author_external_id == "10008899"
    assert c1.published_at.year == 2024

    assert c2.external_comment_id == "fb_1102"
    assert c2.author_display_name == "Bobur Mirzo"


def test_extract_posts_from_json():
    sample_feed_json = {
        "timeline_feed_units": {
            "edges": [
                {
                    "node": {
                        "comet_sections": {
                            "content": {
                                "story": {
                                    "post_id": "post_older",
                                    "message": {"text": "Eski xabar mazmuni."},
                                    "url": "https://www.facebook.com/zuck/posts/post_older",
                                    "creation_time": 1714000000,
                                    "feedback": {
                                        "reaction_count": {"count": 450},
                                        "comments_count_summary": {"total_count": 30},
                                    },
                                }
                            }
                        }
                    }
                },
                {
                    "node": {
                        "comet_sections": {
                            "content": {
                                "story": {
                                    "post_id": "post_newer",
                                    "message": {"text": "Eng yangi texnologik e'lon."},
                                    "url": "https://www.facebook.com/zuck/posts/post_newer",
                                    "creation_time": 1715000000,
                                    "feedback": {
                                        "reaction_count": {"count": 1200},
                                        "comments_count_summary": {"total_count": 95},
                                    },
                                }
                            }
                        }
                    }
                },
            ]
        }
    }
    posts = extract_posts_from_json(sample_feed_json)
    assert len(posts) == 2
    # Verify newest post is first
    assert posts[0]["shortcode"] == "post_newer"
    assert posts[0]["like_count"] == 1200
    assert posts[0]["comment_count"] == 95
    assert "Eng yangi" in posts[0]["caption"]

    assert posts[1]["shortcode"] == "post_older"
    assert posts[1]["like_count"] == 450


def test_extract_posts_merging_same_node():
    """Verify fragments for the same post merge caption and timestamp properly."""
    sample_json = {
        "unit1": {
            "story": {
                "post_id": "post_split",
                "creation_time": 1720000000,
                "url": "https://www.facebook.com/posts/post_split",
            }
        },
        "unit2": {
            "story": {
                "post_id": "post_split",
                "message": {"text": "Post split across two payload nodes."},
                "url": "https://www.facebook.com/posts/post_split",
            }
        },
    }
    posts = extract_posts_from_json(sample_json)
    assert len(posts) == 1
    assert posts[0]["shortcode"] == "post_split"
    assert posts[0]["caption"] == "Post split across two payload nodes."
    assert posts[0]["published_at"] is not None
    assert posts[0]["timestamp"] == 1720000000


@pytest.mark.asyncio
async def test_facebook_collector_fetch_comments_mocked(monkeypatch):
    """Test fetch_comments yields NormalizedComments from initial HTML embedded JSON."""
    collector = FacebookCollector()
    target = MonitoredTarget(
        id="t-1",
        platform="facebook",
        target_type="post_url",
        external_id="10114294029482931",
        url="https://www.facebook.com/zuck/posts/10114294029482931",
        title_or_name="Mark Zuckerberg Post",
    )

    fake_html = """
    <html>
    <head>
    <script type="application/json">
    {
        "feedback": {
            "id": "feedback_10114294029482931",
            "comments": {
                "edges": [
                    {
                        "node": {
                            "id": "c_1",
                            "legacy_fbid": "10001",
                            "body": {"text": "Fikr bildirish uchun juda yaxshi post."},
                            "author": {"id": "user_1", "name": "Test Foydalanuvchi"},
                            "created_time": 1714500000
                        }
                    }
                ]
            }
        }
    }
    </script>
    </head>
    </html>
    """

    class FakeResponse:
        status_code = 200
        text = fake_html

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def head(self, *args, **kwargs):
            return FakeResponse()

        async def get(self, *args, **kwargs):
            return FakeResponse()

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    comments = []
    async for c in collector.fetch_comments(target, limit=10):
        comments.append(c)

    assert len(comments) == 1
    assert comments[0].external_comment_id == "fb_10001"
    assert comments[0].content_text == "Fikr bildirish uchun juda yaxshi post."
    assert comments[0].author_display_name == "Test Foydalanuvchi"


@pytest.mark.asyncio
async def test_facebook_profile_numeric_id_and_login_error_surfaces_playwright(monkeypatch):
    from src.collectors.base import ScraperError

    collector = FacebookCollector()

    class FakeResponse:
        status_code = 200
        text = '<html><head><meta property="og:title" content="Facebook"></head><body></body></html>'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, *args, **kwargs):
            assert "profile.php?id=61592582919537" in str(url)
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    # When Playwright cannot extract posts, it should raise 401 ScraperError and surface any Playwright fallback detail
    with pytest.raises(ScraperError) as exc_info:
        await collector.fetch_profile_info_and_posts("https://www.facebook.com/profile.php?id=61592582919537")

    assert exc_info.value.status_code == 401
    assert "requires login or active burner session" in str(exc_info.value.message)

