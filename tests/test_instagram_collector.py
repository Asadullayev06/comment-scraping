import pytest
from instagrapi import Client
from src.collectors.instagram import InstagramCollector, build_instagram_comment_url
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget


def test_instagram_comment_url_builder():
    url = build_instagram_comment_url("DewaVyCEfCH", "180123456789")
    assert url == "https://www.instagram.com/p/DewaVyCEfCH/c/180123456789/"


def test_media_pk_from_code_offline():
    cl = Client()
    pk = cl.media_pk_from_code("DewaVyCEfCH")
    assert str(pk) == "4012823114027233415"


def test_safe_init_sessionid_no_crash():
    collector = InstagramCollector()
    cl = Client()
    sessionid = "60042763135%3AVWlNHmp9Ib88V2%3A12"
    collector._safe_init_sessionid(cl, sessionid)

    assert cl.private.cookies.get("sessionid") == sessionid
    assert cl.private.cookies.get("ds_user_id") == "60042763135"
    assert "Authorization" in cl.private.headers
    assert cl.private.headers["Authorization"].startswith("Bearer IGT:2:")


@pytest.mark.asyncio
async def test_instagram_fetch_no_pool_manager():
    collector = InstagramCollector(pool_manager=None)
    target = MonitoredTarget(
        id="test-ig",
        platform="instagram",
        target_type="post_url",
        external_id="DewaVyCEfCH",
    )
    comments = []
    async for c in collector.fetch_comments(target):
        comments.append(c)
    assert len(comments) == 0
