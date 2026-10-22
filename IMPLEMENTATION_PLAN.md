# Phased Implementation Roadmap
## Step-by-Step Build Guide for AI Coding Agents

> **How to use this document:** Build this system sequentially from **Phase 1** through **Phase 6**. Do not skip phases. Each phase must pass its verification checks before moving to the next phase. Always consult [ARCHITECTURE.md](file:///c:/Users/s.asadullayev/Desktop/operatives/ARCHITECTURE.md) for exact schemas, prompts, and architectural patterns.

---

## Phase 1: Core Foundation, Database Schema & Docker Infrastructure

### Goal
Establish the Python 3.12 project structure, async PostgreSQL 16 + SQLAlchemy 2.0 models, Alembic migrations, Redis connection, and Coolify-ready `docker-compose.yml`.

### Files to Create
1. `pyproject.toml` — Dependencies:
   - `sqlalchemy[asyncio]>=2.0.30`, `asyncpg>=0.29.0`, `alembic>=1.13.0`
   - `pydantic>=2.7.0`, `pydantic-settings>=2.3.0`
   - `redis>=5.0.0`, `arq>=0.26.0`
   - `aiogram>=3.7.0`, `telethon>=1.36.0`
   - `openai>=1.30.0` (used for DeepSeek API compatibility)
   - `playwright>=1.44.0`, `httpx[http2,socks]>=0.27.0`
   - `cryptography>=42.0.0`, `structlog>=24.1.0`
2. `.env.example` — Complete environment variable template.
3. `src/config.py` — Typed `Settings(BaseSettings)` class loading all env vars.
4. `src/db/base.py` — Async SQLAlchemy engine, `async_sessionmaker`, and `Base`.
5. `src/db/models.py` — Exact implementations of:
   - `MonitoredTarget` (`monitored_targets`)
   - `PlatformAuthor` (`platform_authors`)
   - `Comment` (`comments`)
   - `ScraperSession` (`scraper_sessions`)
6. `src/db/repositories/` — Async CRUD repositories:
   - `TargetRepository` (add, list, update cursor, pause/resume)
   - `CommentRepository` (bulk upsert with `ON CONFLICT (platform, external_comment_id) DO NOTHING`, fetch pending for LLM, update LLM verdicts, query by author)
   - `AuthorRepository` (upsert author metadata, resolve by username or `external_user_id`)
   - `SessionRepository` (Fernet encrypt/decrypt cookies, acquire healthiest session for platform, mark cooldown)
7. `src/schemas/comment.py` — `NormalizedComment` Pydantic model returned by all 5 collectors.
8. `alembic.ini` & `migrations/` — Initial schema migration script.

### Acceptance Criteria
- Running `alembic upgrade head` creates all 4 tables and indexes cleanly.
- `CommentRepository.bulk_ingest()` deduplicates repeated comments without raising integrity errors.

---

## Phase 2: DeepSeek AI Classification Pipeline & Micro-Batcher

### Goal
Implement the cost-optimized, noise-free LLM classification engine using DeepSeek (`deepseek-chat`), automatic prefix caching, and 40-comment micro-batching.

### Files to Create
1. `src/schemas/llm.py` — Pydantic models for batch evaluation input and strict JSON response validation (`BatchEvaluationResponse`, `CommentVerdict`).
2. `src/llm/prompts.py` — The cache-optimized static system prompt (>1,024 tokens with detailed rubric and multilingual examples so DeepSeek's automatic prefix cache activates).
3. `src/llm/client.py` — `DeepSeekClassifier` wrapping `AsyncOpenAI(api_key=settings.deepseek_api_key, base_url="https://api.deepseek.com")`:
   - Uses `response_format={"type": "json_object"}` and `temperature=0.0`.
   - Logs `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` from DeepSeek's response usage metadata.
   - Implements automatic retry with exponential backoff and fallback split-batching if a malformed JSON response occurs.
4. `src/llm/processor.py` — Pre-filter + Micro-Batch Processor:
   - Filters out emoji-only or `<2 word` comments before API calls (`skipped_prefilter`).
   - Groups pending comments into batches of up to `LLM_BATCH_SIZE` (default `40`).
   - Updates `comments` rows in PostgreSQL and pushes any comment where `is_severely_negative == True` and `severity_score >= LLM_ALERT_SEVERITY_THRESHOLD` to the Redis alert queue.

### Acceptance Criteria
- Unit tests verify that casual complaints, off-topic arguments, and mild criticism receive `severely_negative = False`.
- Unit tests verify that batching 100 comments results in at most 3 API calls to DeepSeek.

---

## Phase 3: Telegram Bot Control Plane & Alert Dispatcher (`Aiogram 3`)

### Goal
Build the operator-facing Telegram Bot for managing targets, viewing system status, receiving real-time structured alerts, and triggering user lookups.

### Files to Create
1. `src/bot/middlewares.py` — `AdminAuthMiddleware` rejecting any user whose Telegram ID is not in `ADMIN_TELEGRAM_IDS`.
2. `src/bot/alert_dispatcher.py` — Background task pulling flagged comments from Redis and sending HTML-formatted alerts with:
   - Platform icon & name, source name, severity score
   - Author display name, `@username`, and `<code>numeric_id</code>`
   - Exact comment text inside `<blockquote>` (properly HTML-escaped via `html.escape`)
   - 1-sentence DeepSeek reason
   - Direct hyperlinks to both `post_url` and `comment_url`
   - Inline keyboard buttons (`[🔍 User History]`, `[✅ False Positive]`)
   - Token-bucket rate limiter (max 1 alert every 1.2s per admin chat to avoid Telegram `RetryAfter`).
3. `src/bot/handlers/targets.py` — Commands:
   - `/add_target <url_or_tg_handle>` (auto-detects platform, validates URL/handle, inserts into `monitored_targets`, enqueues scrape job)
   - `/targets` (interactive inline pagination to pause, resume, force-scrape, or delete targets)
4. `src/bot/handlers/accounts.py` — Commands to upload/paste burner cookies JSON and proxy URLs for IG/FB/X (`/add_session`, `/sessions`).
5. `src/bot/handlers/settings.py` — `/start`, `/stats`, `/threshold <1-10>`.
6. `src/bot/main.py` — Bot runner starting both Aiogram polling and the `AlertDispatcher` background loop.

### Acceptance Criteria
- Unauthorized Telegram users receive no response.
- Flagged comments render cleanly in Telegram with clickable deep links and never fail due to unescaped `<`, `>`, or `&` characters in user comments.

---

## Phase 4: Telegram MTProto Collector & User History Lookup Engine (`Telethon`)

### Goal
Implement real-time monitoring of third-party Telegram groups/channel discussions (without admin rights) and the `/lookup` user history feature across all mutual groups.

### Files to Create
1. `scripts/init_telegram_session.py` — Interactive CLI helper to authenticate the Telethon userbot once via phone/QR code and generate the persistent `.session` file (or StringSession).
2. `src/collectors/telegram_mtproto.py` — `TelegramMTProtoService`:
   - **Real-Time Listener:** Subscribes to `events.NewMessage` across all active `telegram_chat` targets (automatically resolving linked discussion groups for broadcast channels via `GetFullChannelRequest`).
   - **Deep Link Builder:** Constructs exact `https://t.me/...` or `https://t.me/c/...` links to both the post and the comment.
   - **User History Lookup (`lookup_user_history`):**
     1. Accepts `@username` or numeric ID.
     2. Resolves target `InputPeerUser` via Telethon or local `platform_authors` cache.
     3. Calls `GetCommonChatsRequest(user_id=..., max_id=0, limit=100)` to discover all mutual groups/supergroups, plus combines with all joined/monitored discussion groups.
     4. Iterates through each chat using `client.iter_messages(chat, from_user=target_user, limit=500)` with a safe `1.0s` inter-chat delay.
     5. Upserts all discovered messages into PostgreSQL.
     6. If `filter_mode == "negative"`, runs any unevaluated messages from that user through `DeepSeekClassifier` immediately.
     7. Returns the unified list of comments to the Bot handler.
3. `src/bot/handlers/lookup.py` — Implements `/lookup <@username|user_id> [all|negative]`:
   - Shows live progress message (`"🔎 Scanning 14 mutual groups for @user..."`).
   - Displays paginated results in Telegram AND generates a downloadable self-contained HTML report file when total comments > 10.

### Acceptance Criteria
- Posting a comment in a monitored third-party group/discussion board is ingested within 2 seconds, evaluated by DeepSeek within 5 seconds, and alerted in the Bot.
- Running `/lookup @username all` or `/lookup 123456789 negative` returns historical messages across all mutual groups with clickable message links.

---

## Phase 5: High-Volume Social Collectors (YouTube, X, Instagram, Facebook)

### Goal
Build the cursor-paginated collectors capable of extracting 5,000–6,000+ comments per post without DOM crashes, backed by the Session & Proxy Pool Manager.

### Files to Create
1. `src/collectors/base.py` — `BaseCollector` abstract class with standardized `fetch_comments(target: MonitoredTarget) -> AsyncIterator[NormalizedComment]`.
2. `src/collectors/pool_manager.py` — `SessionPoolManager`:
   - Selects active `ScraperSession` with lowest recent usage and valid cooldown status.
   - Provides pre-configured Playwright browser context / `httpx.AsyncClient` with the session's decrypted cookies, pinned `User-Agent`, and `proxy_url`.
   - Automatically puts a session into cooldown if HTTP `429` or checkpoint challenge is detected.
3. `src/collectors/youtube.py` — `YouTubeCollector`:
   - Primary: Async HTTPX client calling YouTube Data API v3 `commentThreads` + `comments.list` (for replies), 100 per page.
   - Fallback: InnerTube `/youtubei/v1/next` continuation token extractor (zero API quota needed).
4. `src/collectors/twitter_x.py` — `TwitterXCollector`:
   - Uses Playwright/HTTPX with burner session cookies (`auth_token`, `ct0`) to query `TweetDetail` GraphQL endpoint with cursor pagination and `ShowMoreThreads` expansion.
5. `src/collectors/instagram.py` — `InstagramCollector`:
   - Uses authenticated burner session to paginate `/api/v1/media/{media_id}/comments/` and child comment threads (`/inline_child_comments/`) up to 6,000+ comments with `1.5s–3.5s` jitter.
6. `src/collectors/facebook.py` — `FacebookCollector`:
   - Uses authenticated Playwright context to intercept Comet GraphQL (`RANKED_UNFILTERED` mode) and paginate `feedbackTargetID` cursors.
7. `src/workers/worker_settings.py` — Arq worker cron + on-demand task definitions orchestrating periodic polling and batch ingestion.

### Acceptance Criteria
- Each collector yields `NormalizedComment` objects with accurate `comment_url`, `post_url`, `author_username`, `author_external_id`, and `content_text`.
- High-water mark deduplication stops pagination early on subsequent runs once previously ingested comments are reached.

---

## Phase 6: Coolify Packaging, End-to-End Verification & Hardening

### Goal
Finalize Dockerfiles, `docker-compose.yml`, healthchecks, and operator documentation for seamless deployment on Coolify.

### Files to Create
1. `Dockerfile` — Multi-stage build based on `mcr.microsoft.com/playwright/python:v1.44.0-jammy` (or slim image for non-browser services).
2. `docker-compose.yml` — Production Coolify compose stack defining `postgres`, `redis`, `migrator`, `bot-controller`, `telegram-collector`, `scraper-worker`, and `llm-worker` with restart policies, volume mounts, and resource limits.
3. `README.md` — Operator quickstart guide (how to get Telegram `API_ID`/`API_HASH`, initialize Telethon session, add burner cookies, and deploy on Coolify).
