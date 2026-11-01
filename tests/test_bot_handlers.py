"""Unit tests for bot URL parsing and report generation."""

from src.bot.handlers.targets import detect_platform_and_id
from src.bot.handlers.lookup import generate_html_report


def test_detect_platform_and_id():
    # Telegram handle
    p, t, eid, url = detect_platform_and_id("@my_channel")
    assert p == "telegram"
    assert t == "telegram_chat"
    assert eid == "@my_channel"
    assert url == "https://t.me/my_channel"

    # Telegram link
    p, t, eid, url = detect_platform_and_id("https://t.me/some_group")
    assert p == "telegram"
    assert eid == "some_group"

    # YouTube
    p, t, eid, url = detect_platform_and_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert p == "youtube"
    assert t == "post_url"
    assert eid == "dQw4w9WgXcQ"

    # Instagram
    p, t, eid, url = detect_platform_and_id("https://www.instagram.com/p/C7XyZ12345/")
    assert p == "instagram"
    assert t == "post_url"
    assert eid == "C7XyZ12345"

    # X
    p, t, eid, url = detect_platform_and_id("https://x.com/elonmusk/status/1792345678901234567")
    assert p == "x"
    assert t == "post_url"
    assert eid == "1792345678901234567"

    # Facebook post URLs
    p, t, eid, _ = detect_platform_and_id("https://www.facebook.com/username/posts/10114294029482931")
    assert p == "facebook"
    assert t == "post_url"
    assert eid == "10114294029482931"

    p, t, eid, _ = detect_platform_and_id("https://www.facebook.com/permalink.php?story_fbid=pfbid02G7w8xYq9pZ&id=10001")
    assert p == "facebook"
    assert t == "post_url"
    assert eid == "pfbid02G7w8xYq9pZ"

    p, t, eid, _ = detect_platform_and_id("https://www.facebook.com/watch/?v=1122334455")
    assert p == "facebook"
    assert t == "post_url"
    assert eid == "1122334455"

    p, t, eid, _ = detect_platform_and_id("https://fb.watch/abcd1234/")
    assert p == "facebook"
    assert t == "post_url"
    assert eid == "abcd1234"

    # Facebook profile URLs
    p, t, eid, _ = detect_platform_and_id("https://www.facebook.com/zuck")
    assert p == "facebook"
    assert t == "profile"
    assert eid == "zuck"

    p, t, eid, _ = detect_platform_and_id("https://www.facebook.com/profile.php?id=4")
    assert p == "facebook"
    assert t == "profile"
    assert eid == "4"


def test_generate_html_report():
    comments = [
        {
            "id": "c1",
            "published_at": "2026-01-01 12:00:00",
            "source": "Monitored Group",
            "severity_score": 8,
            "text": "Critical comment <test>",
            "llm_reason": "Opposes regulations",
            "url": "https://t.me/c/1/2",
        }
    ]
    html_doc = generate_html_report("Test User", "12345", "negative", comments)
    assert "<!DOCTYPE html>" in html_doc
    assert "Test User" in html_doc
    assert "&lt;test&gt;" in html_doc  # escaped
    assert "8/10" in html_doc
