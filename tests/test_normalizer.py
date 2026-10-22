"""Unit tests for comment normalization, URL builders, and HTML escaping."""

from datetime import datetime, timezone
from src.schemas.comment import NormalizedComment
from src.schemas.alert import AlertPayload
from src.collectors.telegram_mtproto import clean_channel_id, build_telegram_message_url
from src.bot.alert_dispatcher import format_alert_html


def test_clean_channel_id():
    assert clean_channel_id(-1001234567890) == 1234567890
    assert clean_channel_id(-987654321) == 987654321
    assert clean_channel_id(12345) == 12345


def test_build_telegram_message_url():
    # Public group
    url = build_telegram_message_url("mygroup", -1001234567890, 42)
    assert url == "https://t.me/mygroup/42"

    # Private group
    url = build_telegram_message_url(None, -1001234567890, 42)
    assert url == "https://t.me/c/1234567890/42"

    # Discussion comment under channel post
    url = build_telegram_message_url(
        chat_username=None,
        chat_id=-1001234567890,
        message_id=999,
        channel_post_id=55,
        channel_username="news_channel",
    )
    assert url == "https://t.me/news_channel/55?comment=999"


def test_normalized_comment_schema():
    c = NormalizedComment(
        platform="telegram",
        external_comment_id="tg_123_456",
        author_external_id="111222333",
        author_username="testuser",
        author_display_name="Test User",
        content_text="This is a test comment",
        published_at=datetime.now(timezone.utc),
    )
    assert c.platform == "telegram"
    assert c.author_username == "testuser"
    assert c.content_text == "This is a test comment"


def test_format_alert_html_strict_escaping():
    alert = AlertPayload(
        comment_id="test-uuid",
        platform="telegram",
        source_name="<Evil & Malicious Group>",
        post_url="https://t.me/post/1",
        comment_url="https://t.me/post/1?comment=2",
        author_external_id="12345",
        author_username="bad<user>&co",
        author_display_name="Hacker <script>alert(1)</script>",
        content_text="Why is the law <terrible> & corrupt? <b>Defy them!</b>",
        severity_score=9,
        llm_reason="Incites defiance & calls the law <terrible>.",
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    html_out = format_alert_html(alert)

    # Assert raw unescaped HTML characters are NOT in output
    assert "<Evil & Malicious Group>" not in html_out
    assert "&lt;Evil &amp; Malicious Group&gt;" in html_out
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "&lt;terrible&gt; &amp; corrupt?" in html_out
    assert "&lt;b&gt;Defy them!&lt;/b&gt;" in html_out
