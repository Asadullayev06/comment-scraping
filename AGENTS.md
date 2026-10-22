# AI Agent Engineering Instructions (`AGENTS.md`)

> **CRITICAL:** Any AI coding model working in this repository MUST read and obey these rules, along with [ARCHITECTURE.md](file:///c:/Users/s.asadullayev/Desktop/operatives/ARCHITECTURE.md) and [IMPLEMENTATION_PLAN.md](file:///c:/Users/s.asadullayev/Desktop/operatives/IMPLEMENTATION_PLAN.md), before writing or modifying code.

## 1. Non-Negotiable Architectural Rules

1. **No Single-Comment LLM Calls in Production Loops:**
   - Never call the DeepSeek API inside a `for comment in comments:` loop one comment at a time.
   - Always route comments through the **Micro-Batch Processor (`src/llm/processor.py`)** in batches of up to 40 comments (`LLM_BATCH_SIZE=40`) with `response_format={"type": "json_object"}`.
2. **Preserve DeepSeek Prefix Cache:**
   - Never inject dynamic variables (like timestamps, post URLs, or target names) into the **System Prompt** (`src/llm/prompts.py`).
   - Keep the System Prompt 100% static so DeepSeek's automatic Context Caching gives a 90% discount on system prompt tokens. Put all dynamic context and the comment array inside the `user` message.
3. **No Infinite DOM Scrolling & Dedicated Account Pinning for 5,000+ Comment Posts:**
   - For Instagram, Facebook, and X (Twitter), never rely solely on `page.mouse.wheel()` or scrolling DOM elements to load 5,000+ comments.
   - For Instagram: Use `instagrapi` mobile API endpoints with Android signatures rather than fragile web headers. Assign a single burner account to paginate a target post start to finish with `2.0s–4.0s` jitter (do NOT hand off cursors across multiple accounts on the same post).
   - Track `child_comment_count` on parent comments. Re-fetch child replies if `child_comment_count` increases on rescrape.
   - Catch `ChallengeRequired`, `FeedbackRequired`, `PleaseWaitFewMinutes`, and `LoginRequired`; pause the affected account for 6 hours, alert the operator in Telegram, and let remaining accounts continue.
4. **Strict HTML Escaping in Telegram Alerts:**
   - All Telegram alerts use `ParseMode.HTML`.
   - Always pass untrusted fields (`content_text`, `display_name`, `source_name`, `llm_reason`) through `html.escape()` before inserting them into the HTML alert template so comments containing `<` or `&` never crash `bot.send_message()`.
5. **Read-Only MTProto Userbot:**
   - The Telethon userbot (`src/collectors/telegram_mtproto.py`) is strictly a **passive observer and search client**.
   - Never call `client.send_message()`, `client.send_reaction()`, or modify group state from the MTProto userbot account. All user interaction happens exclusively through the Aiogram Bot (`TELEGRAM_BOT_TOKEN`).
6. **Database Deduplication:**
   - Every collector must populate `external_comment_id` and `platform`.
   - Database inserts into `comments` must use PostgreSQL `INSERT ... ON CONFLICT (platform, external_comment_id) DO NOTHING` so re-scraping a post never creates duplicate comments or duplicate LLM calls.

## 2. Build Order for Coding Models

When asked to implement or continue building the project, check which phase of [IMPLEMENTATION_PLAN.md](file:///c:/Users/s.asadullayev/Desktop/operatives/IMPLEMENTATION_PLAN.md) is currently incomplete:
- **Phase 1:** Project skeleton, `pyproject.toml`, SQLAlchemy 2.0 async models, repositories, Alembic migrations.
- **Phase 2:** DeepSeek API client, cache-optimized system prompt, 40-comment micro-batch processor.
- **Phase 3:** Aiogram 3 Bot controller, admin middleware, target management commands, rate-limited HTML alert dispatcher.
- **Phase 4:** Telethon MTProto real-time group/discussion listener + `/lookup` mutual-group deep user history scanner.
- **Phase 5:** Session/Proxy Pool Manager + YouTube, X, Instagram, and Facebook cursor-paginated collectors + Arq worker jobs.
- **Phase 6:** Dockerfile, Coolify `docker-compose.yml`, and operator documentation.
