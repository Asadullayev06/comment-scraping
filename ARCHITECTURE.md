# Multi-Platform Negative Comment Monitoring & Reporting System
## Master System Architecture & Engineering Specification

> **Document Purpose:** This document is the single source of truth for architecting and building the Multi-Platform Negative Comment Monitoring & Reporting System. Subsequent AI coding models and engineers must follow the specifications, schemas, pipelines, and patterns defined here.

---

## 1. Executive Summary & Core Objectives

The system is a self-hosted, centralized OSINT comment monitoring, AI classification, and real-time alerting platform operated entirely through a **Telegram Bot Control Plane** and deployed via **Coolify**.

### Core Capabilities
1. **Multi-Platform Comment Ingestion (Without Admin Access):**
   - **Telegram:** Real-time monitoring of third-party public/private groups and channel discussion boards where the monitoring account is a regular participant (via MTProto Userbot).
   - **YouTube, Instagram, Facebook, X (Twitter):** High-volume post/video comment tracking capable of extracting **5,000–6,000+ comments per target post** using self-hosted headless browser network interception (GraphQL/internal APIs), rotating proxies, and burner session pools.
2. **Noise-Free AI Filtering (DeepSeek API):**
   - Evaluates all ingested comments via the **DeepSeek API (`deepseek-chat` / DeepSeek-V3)** using **prompt prefix caching** and **micro-batching (40 comments per request)** to minimize token expenditure by >85% while maintaining low latency.
   - Ignores casual complaining, spam, off-topic arguments, or mild criticism; flags **only** comments that are on-topic and express severe negativity, strong hostility, or direct opposition toward government rules, policies, regulations, or institutions.
3. **Telegram User History Lookup (`@username` or Numeric ID):**
   - On-demand investigation command inside the Telegram Bot.
   - Combines instant lookup from the local PostgreSQL archive with a live **MTProto Deep Scan** (`GetCommonChatsRequest` + `iter_messages(from_user=...)`) across all mutual groups and monitored channels, returning either **all historical comments** or **only LLM-verified negative comments**.
4. **Centralized Real-Time Telegram Reporting:**
   - Delivers structured rich-text alerts with platform metadata, direct deep links to both the post and the exact comment, author identity (`@username`, display name, numeric ID), exact comment text, and a 1-sentence LLM justification.
5. **Coolify-Native Deployment:**
   - Packaged as a multi-container Docker Compose stack with persistent volumes, health checks, and environment-driven configuration ready for one-click deployment on Coolify.

---

## 2. Definitive Technology Stack

To maximize reliability, scraping resilience, and MTProto compatibility, the entire backend is standardized on **Python 3.12+ (Asyncio)**.

| Layer | Technology | Rationale |
| :--- | :--- | :--- |
| **Runtime** | **Python 3.12+** | First-class ecosystem for Telegram MTProto, Playwright, async task queues, and LLM SDKs. |
| **Telegram Userbot (MTProto)** | **Telethon 1.36+** | Most mature MTProto library for listening to unowned groups/discussion channels, resolving numeric IDs, discovering mutual chats (`GetCommonChatsRequest`), and server-side user message searching (`iter_messages`). |
| **Telegram Control & Alert Bot** | **Aiogram 3.x** | Modern async Telegram Bot framework with FSM (Finite State Machine) for interactive menus, target management, and rate-limited alert delivery. |
| **Social Scrapers (IG, FB, X)** | **Playwright (Async) + HTTPX** | Uses headless Chromium with stealth patches to authenticate and intercept internal **GraphQL / REST pagination cursors**, enabling extraction of 6,000+ comments without DOM memory crashes. |
| **YouTube Extractor** | **YouTube Data API v3 + InnerTube Fallback** | Uses official API (10,000 free units/day = ~1M comments/day) with automatic fallback to self-hosted InnerTube continuation-token scraping if quota is exceeded. |
| **LLM Classification Engine** | **DeepSeek API (`openai` Async SDK)** | Uses `deepseek-chat` (DeepSeek-V3) with JSON mode, **Automatic Context Caching** on the static system prompt, and **40-comment micro-batching**. |
| **Primary Database** | **PostgreSQL 16** | Stores monitored targets, ingested comments, deduplication hashes, author profiles, scraper session cookies, and proxy health metrics. |
| **ORM & Migrations** | **SQLAlchemy 2.0 (Async) + Alembic** | Type-safe async database operations via `asyncpg` with reproducible schema migrations. |
| **Task Queue & Cache** | **Redis 7 + Arq (Asyncio Redis Queue)** | Lightweight, native-asyncio job queue for scheduling scraper runs, buffering comments into micro-batches for DeepSeek, and enforcing alert rate limits. |
| **Deployment Target** | **Docker Compose on Coolify** | Multi-service stack (`postgres`, `redis`, `bot-controller`, `telegram-listener`, `scraper-worker`, `llm-worker`) managed via Coolify. |

---

## 3. High-Level System Architecture

```mermaid
flowchart TB
    subgraph Admin["Operator Interface"]
        TG_ADMIN["Operator Telegram Client"]
    end

    subgraph ControlPlane["Control & Alerting Plane (Aiogram 3)"]
        BOT["Telegram Bot Controller<br/>Commands, Target Mgmt, History UI, Alert Dispatcher"]
    end

    subgraph IngestionPlane["Multi-Platform Ingestion Plane"]
        TG_USERBOT["Telegram MTProto Collector (Telethon)<br/>Real-time Event Listener + On-Demand Deep Scanner"]
        YT_WORKER["YouTube Collector<br/>Data API v3 + InnerTube Fallback"]
        X_WORKER["X / Twitter Collector<br/>GraphQL TweetDetail Cursor Engine"]
        IG_WORKER["Instagram Collector<br/>GraphQL / Web API Comment Paginator"]
        FB_WORKER["Facebook Collector<br/>Comet GraphQL Feedback Paginator"]
        POOL_MGR["Session & Proxy Pool Manager<br/>Cookie Vault, Proxy Rotation, Cooldowns"]
    end

    subgraph DataAndQueue["Persistence & Messaging Layer"]
        REDIS[("Redis 7<br/>Arq Job Queue + LLM Batch Buffer + Dedup Cache")]
        PG[("PostgreSQL 16<br/>Targets, Comments, Authors, Sessions, Audit Logs")]
    end

    subgraph IntelligencePlane["AI Filtering Plane"]
        LLM_BATCHER["Micro-Batch Aggregator<br/>Groups 40 Comments or 5s Window"]
        DEEPSEEK["DeepSeek API (deepseek-chat)<br/>Cached System Prompt + Structured JSON Output"]
    end

    TG_ADMIN <-->|"Commands & Alerts"| BOT
    BOT <--> PG
    BOT <--> REDIS

    TG_USERBOT -->|"New Messages & History Scans"| REDIS
    YT_WORKER & X_WORKER & IG_WORKER & FB_WORKER <--> POOL_MGR
    YT_WORKER & X_WORKER & IG_WORKER & FB_WORKER -->|"Normalized Comments"| REDIS

    REDIS -->|"Persist Raw/Normalized"| PG
    REDIS --> LLM_BATCHER
    LLM_BATCHER <-->|"Batch Classify (JSON)"| DEEPSEEK
    LLM_BATCHER -->|"Update Verdict & Score"| PG
    LLM_BATCHER -->|"Flagged Severe Comments"| REDIS
    REDIS -->|"Dispatch Alert Queue"| BOT
```

---

## 4. Repository Directory Structure

Future models must initialize and maintain the project using this exact modular layout:

```text
operatives/
├── docker-compose.yml              # Production Coolify Docker Compose stack
├── Dockerfile                      # Multi-stage Python + Playwright Chromium image
├── pyproject.toml                  # Dependencies (uv / poetry / pip compatible)
├── .env.example                    # Template for all required environment variables
├── alembic.ini                     # Alembic migration config
├── migrations/                     # Database schema migrations
│   └── versions/
├── src/
│   ├── __init__.py
│   ├── config.py                   # Pydantic Settings (env vars validation)
│   ├── db/
│   │   ├── base.py                 # SQLAlchemy 2.0 DeclarativeBase & async engine
│   │   ├── models.py               # All PostgreSQL table definitions
│   │   └── repositories/           # Data access layer (targets, comments, users, sessions)
│   ├── schemas/
│   │   ├── comment.py              # Unified NormalizedComment Pydantic schema
│   │   ├── llm.py                  # DeepSeek request/response JSON schemas
│   │   └── alert.py                # Alert payload schema
│   ├── collectors/
│   │   ├── base.py                 # Abstract BaseCollector interface
│   │   ├── pool_manager.py         # Burner account session & rotating proxy manager
│   │   ├── telegram_mtproto.py     # Telethon real-time listener + deep user history scanner
│   │   ├── youtube.py              # YouTube API v3 + InnerTube continuation scraper
│   │   ├── twitter_x.py            # X GraphQL TweetDetail interceptor & cursor paginator
│   │   ├── instagram.py            # Instagram comment API/GraphQL paginator
│   │   └── facebook.py             # Facebook Comet GraphQL comment extractor
│   ├── llm/
│   │   ├── client.py               # DeepSeek AsyncOpenAI client with retry & backoff
│   │   ├── prompts.py              # Cache-optimized system prompt & policy rubric
│   │   └── processor.py            # Micro-batcher (40 comments/batch) & response parser
│   ├── bot/
│   │   ├── main.py                 # Aiogram 3 entrypoint
│   │   ├── middlewares.py          # Admin whitelist auth middleware
│   │   ├── handlers/
│   │   │   ├── targets.py          # /add_target, /list_targets, /pause_target, /remove_target
│   │   │   ├── lookup.py           # /lookup <@username|user_id> [all|negative]
│   │   │   ├── accounts.py         # /accounts, /proxies (health & session management)
│   │   │   └── settings.py         # /policy, /stats, /threshold
│   │   └── alert_dispatcher.py     # Rate-limited alert formatter & sender (token bucket)
│   └── workers/
│       └── worker_settings.py      # Arq worker definitions (scrape jobs, batch LLM jobs)
└── tests/
    ├── test_normalizer.py
    ├── test_llm_batcher.py
    └── test_alert_formatter.py
```

---

## 5. Database Schema Specification (PostgreSQL 16)

All tables use `UUID` or `BIGINT` primary keys and `TIMESTAMPTZ` (UTC) timestamps.

### 5.1 `monitored_targets`
Stores Telegram groups/channels and specific social media posts/videos being tracked.
- `id` (`UUID`, PK)
- `platform` (`ENUM('telegram', 'youtube', 'instagram', 'facebook', 'x')`, indexed)
- `target_type` (`ENUM('telegram_chat', 'post_url', 'account_feed')`)
- `external_id` (`VARCHAR(255)`, unique per platform — e.g., Telegram chat ID `-100...`, YouTube video ID, Tweet ID, IG shortcode, FB post ID)
- `url` (`TEXT`, nullable for private Telegram chats)
- `title_or_name` (`TEXT` — channel/group/page title)
- `poll_interval_seconds` (`INTEGER`, default `300` for posts, `0` for real-time Telegram stream)
- `last_cursor` (`TEXT`, nullable — stores pagination token or highest seen comment ID/timestamp)
- `last_scraped_at` (`TIMESTAMPTZ`, nullable)
- `total_comments_collected` (`INTEGER`, default `0`)
- `is_active` (`BOOLEAN`, default `true`)
- `created_at` (`TIMESTAMPTZ`, default `now()`)

### 5.2 `platform_authors`
Stores normalized author profiles across platforms, enabling fast cross-group lookup on Telegram.
- `id` (`UUID`, PK)
- `platform` (`ENUM('telegram', 'youtube', 'instagram', 'facebook', 'x')`)
- `external_user_id` (`VARCHAR(128)` — e.g., Telegram numeric ID `123456789`, X rest_id, IG pk)
- `username` (`VARCHAR(255)`, indexed lowercase without `@`)
- `display_name` (`TEXT`)
- `profile_url` (`TEXT`, nullable)
- `first_seen_at` (`TIMESTAMPTZ`)
- `last_seen_at` (`TIMESTAMPTZ`)
- `total_comments` (`INTEGER`, default `0`)
- `flagged_comments_count` (`INTEGER`, default `0`)
- **Unique Constraint:** `(platform, external_user_id)`
- **Index:** `idx_authors_platform_username ON (platform, lower(username))`

### 5.3 `comments`
Stores every ingested comment, its LLM classification result, and alert status.
- `id` (`UUID`, PK)
- `platform` (`ENUM('telegram', 'youtube', 'instagram', 'facebook', 'x')`)
- `target_id` (`UUID`, FK -> `monitored_targets.id`, indexed)
- `author_id` (`UUID`, FK -> `platform_authors.id`, indexed)
- `external_comment_id` (`VARCHAR(255)` — platform-native message/comment ID)
- `parent_external_id` (`VARCHAR(255)`, nullable — post ID or parent comment ID)
- `source_name` (`TEXT` — snapshot of group/channel/page name)
- `post_url` (`TEXT`)
- `comment_url` (`TEXT` — direct deep link to the exact comment)
- `content_text` (`TEXT` — raw comment text)
- `content_hash` (`CHAR(64)` — SHA-256 of `platform + external_comment_id` for O(1) deduplication)
- `published_at` (`TIMESTAMPTZ`, indexed)
- `ingested_at` (`TIMESTAMPTZ`, default `now()`)
- `llm_status` (`ENUM('pending', 'processing', 'evaluated', 'skipped_prefilter', 'failed')`, indexed)
- `is_on_topic` (`BOOLEAN`, nullable)
- `is_severely_negative` (`BOOLEAN`, nullable, indexed)
- `severity_score` (`SMALLINT`, nullable — `0` to `10`)
- `llm_reason` (`TEXT`, nullable — 1-sentence explanation from DeepSeek)
- `alert_sent` (`BOOLEAN`, default `false`)
- `alert_sent_at` (`TIMESTAMPTZ`, nullable)
- **Unique Constraint:** `(platform, external_comment_id)`

### 5.4 `scraper_sessions`
Manages burner accounts, cookies, and authentication states for IG, FB, and X.
- `id` (`UUID`, PK)
- `platform` (`ENUM('instagram', 'facebook', 'x', 'telegram')`)
- `account_identifier` (`VARCHAR(255)`)
- `cookies_encrypted` (`BYTEA` / `TEXT` — Fernet-encrypted JSON cookies/storage state)
- `user_agent` (`TEXT`)
- `proxy_url` (`TEXT`, nullable — pinned residential/mobile proxy for session stickiness)
- `status` (`ENUM('active', 'cooldown', 'challenged', 'banned')`)
- `cooldown_until` (`TIMESTAMPTZ`, nullable)
- `last_used_at` (`TIMESTAMPTZ`, nullable)
- `error_count` (`INTEGER`, default `0`)

---

## 6. Platform Ingestion Engines (Handling 5,000–6,000+ Comments)

### Why DOM Scrolling Fails at 5,000 Comments (And How We Solve It)
Attempting to scroll a browser DOM through 6,000 comments on Instagram, Facebook, or X causes Chromium RAM usage to spike past 4GB and triggers rate limits.
**Architectural Rule:** Every scraper for IG, FB, and X **must use Network Interception & Direct Cursor Pagination**, not infinite DOM scrolling:
1. A headless Playwright session logs in once (or loads saved `storage_state` cookies) and navigates to the target post URL.
2. Playwright intercepts the initial comment GraphQL/API XHR request to capture the **request headers (`x-csrf-token`, `x-ig-app-id`, `x-twitter-client-language`, `fb_dtsg`, `lsd`)** and the **GraphQL `doc_id` / query hash**.
3. Subsequent pages are fetched directly via paginated JSON/GraphQL cursor requests (`httpx` or `page.evaluate(fetch(...))`) at 50–100 comments per page with jittered delays (`1.5s–3.5s`), rotating across the burner session pool if HTTP `429` occurs.
4. **Incremental High-Water Mark:** After the initial full scrape of a 6,000-comment post, the worker switches to sorting by **Newest First** (where supported) and stops pagination as soon as it encounters comments already present in `comments.external_comment_id`.

### 6.1 Telegram Collector (`Telethon` MTProto Userbot)
- **No Admin Required:** Connects as a standard user account using a persistent Telethon `.session` file stored in a mounted Docker volume.
- **Real-Time Stream:** Attaches `client.on(events.NewMessage(chats=monitored_chat_ids))` to capture every message posted in monitored groups and channel discussion groups (`linked_chat_id` of broadcast channels) in real time.
- **Deep Link Generation:**
  - Public group/channel: `https://t.me/{chat_username}/{message_id}`
  - Discussion comment on channel post: `https://t.me/{channel_username}/{post_id}?comment={message_id}`
  - Private group (where user is member): `https://t.me/c/{clean_chat_id}/{message_id}`
- **Author Identity Extraction:** Extracts `sender.id` (numeric ID), `sender.username`, and `first_name + last_name`.

### 6.2 YouTube Collector
- **Primary Path (Official Data API v3):**
  - Calls `commentThreads.list(part="snippet,replies", videoId=VIDEO_ID, maxResults=100, order="time", pageToken=cursor)`.
  - 6,000 comments = 60 API calls = **60 quota units** (out of 10,000 free daily units). Completes in ~15 seconds.
- **Secondary Path (InnerTube Continuation Scraper):**
  - If API quota is exhausted or disabled, extracts `innertubeApiKey` and `continuationCommand.token` from `https://www.youtube.com/watch?v={VIDEO_ID}` and POSTs to `/youtubei/v1/next` to paginate all comments and nested replies without an API key.
- **Deep Link Generation:** `https://www.youtube.com/watch?v={video_id}&lc={comment_id}`

### 6.3 X (Twitter) Collector
- **Mechanism:** Uses authenticated burner sessions with `x-csrf-token` (`ct0` cookie) and `auth_token` cookie to query X's internal GraphQL endpoint:
  - Endpoint: `https://x.com/i/api/graphql/{query_id}/TweetDetail`
  - Variables: `{"focalTweetId": tweet_id, "cursor": cursor, "rankingMode": "Recency", "withCommunity": false, "includePromotedContent": false}`
- **Nested Thread Show-More Expansion:** Parses `TimelineTimelineItem` and `TimelineTimelineModule` entries, extracting `Bottom` and `ShowMoreThreads` cursors to capture all replies.
- **Account Rotation:** Each burner account is capped at 120 GraphQL requests per 15-minute window and pinned to a dedicated proxy.
- **Deep Link Generation:** `https://x.com/{author_username}/status/{reply_tweet_id}`

### 6.4 Instagram Collector
- **Mechanism:** Uses `instagrapi` mobile API (`i.instagram.com`) which emulates genuine Android device profiles, device GUIDs, and signed requests rather than fragile web headers.
- **Dedicated Account Pinning (No Mid-Cursor Handoff):** Assigns a single burner account to paginate a target post start to finish with `2.0s–4.0s` jitter. Does NOT rotate accounts across the same post's pagination sequence, avoiding Meta graph correlation and cursor session invalidation.
- **Child Replies:** Inspects `child_comment_count` on parent comments. Fetches nested replies via `media_comment_replies(media_id, comment_pk)` with safe pacing.
- **Incremental Rescrape Resilience:**
  - Accommodates popularity-ranked feeds by evaluating recently active comments rather than blindly breaking at the first seen ID.
  - Compares `current_child_count > stored_child_count` in PostgreSQL to catch new replies posted under older parent comments.
- **Challenge & Checkpoint Fault-Tolerance:** Catches `ChallengeRequired`, `FeedbackRequired`, `PleaseWaitFewMinutes`, and `LoginRequired`. Pauses the affected burner account for 6 hours, fires an immediate Telegram alert to the operator, and allows other burner accounts to continue working.
- **Deep Link Generation:** `https://www.instagram.com/p/{shortcode}/c/{comment_pk}/`

### 6.5 Facebook Collector
- **Mechanism:** Uses Playwright with authenticated session cookies (`c_user`, `xs`) and intercepts Facebook's Comet GraphQL API (`https://www.facebook.com/api/graphql/`).
- **Sorting Switch:** Forces comment filter mode to `RANKED_UNFILTERED` ("All comments, including potential spam, newest first") via GraphQL variables `{"feedbackTargetID": feedback_id, "commentOrderingMode": "RANKED_UNFILTERED", "count": 50, "cursor": end_cursor}` so no comments are hidden by Facebook's default "Most Relevant" filter.
- **Deep Link Generation:** `https://www.facebook.com/{page_or_post_id}/posts/{post_id}?comment_id={comment_id}`

---

## 7. Smart AI Filtering Pipeline (DeepSeek API Optimization)

Analyzing 5,000–6,000 comments per post one-by-one would require 6,000 HTTP roundtrips and repeat the system prompt 6,000 times. We solve this with a **3-Stage High-Efficiency Pipeline**:

```mermaid
flowchart LR
    RAW["Raw Ingested Comment"] --> DEDUP["Stage 1: Deduplication & Trivial Filter<br/>(Skip empty, emoji-only, <2 words, already evaluated)"]
    DEDUP --> BUFFER["Stage 2: Redis Micro-Batch Buffer<br/>(Pack up to 40 comments OR flush after 4 seconds)"]
    BUFFER --> DEEPSEEK["Stage 3: DeepSeek-V3 Batch Evaluation<br/>(Prefix-Cached System Prompt + Strict JSON Array)"]
    DEEPSEEK --> VERDICT{"is_severely_negative == true<br/>AND severity_score >= 7?"}
    VERDICT -->|"Yes"| ALERT["Enqueue Immediate Telegram Alert"]
    VERDICT -->|"No"| ARCHIVE["Save Verdict in PostgreSQL (Silent)"]
```

### 7.1 Stage 1: Zero-Cost Pre-Filter
Before touching the DeepSeek API:
- Deduplicate via `content_hash` (`platform + external_comment_id`).
- Skip comments containing only emojis, URLs, `@mentions` with no text, or fewer than 2 words (mark as `llm_status = 'skipped_prefilter'`).

### 7.2 Stage 2: Micro-Batching (40 Comments per Request)
- The `llm-worker` pulls up to **40 pending comments** at a time (or flushes after **4 seconds** for real-time Telegram messages so alerts remain near-instant).
- For a 6,000-comment post, 6,000 comments are processed in just **150 DeepSeek API calls** (run with bounded concurrency of 5 parallel requests = completed in ~45 seconds).

### 7.3 Stage 3: DeepSeek Context Caching & Prompt Architecture
DeepSeek automatically caches prompt prefixes (at 0.1x token cost) when the beginning of the prompt is static. Therefore, our **System Prompt** must be 100% static and >1,024 tokens (including detailed classification criteria and few-shot examples), while the dynamic comment batch is passed strictly in the `user` message.

#### Exact System Prompt Specification (`src/llm/prompts.py`)
```text
You are an expert multilingual political and regulatory sentiment analyst.
Your task is to evaluate batches of social media comments and identify ONLY those comments that are:
1. ON-TOPIC: Directly or indirectly referencing government rules, state policies, public regulations, laws, taxes, state officials, ministries, law enforcement, or government institutions.
2. SEVERELY NEGATIVE / HOSTILE: Expressing extreme negativity, strong hostility, incitement, fierce condemnation, delegitimization, or direct opposition toward those government rules, policies, or institutions.

STRICT NOISE REDUCTION RULES (DO NOT FLAG):
- DO NOT flag casual complaining about everyday life, weather, sports, entertainment, or commercial products/services.
- DO NOT flag off-topic insults between two regular users arguing about personal matters.
- DO NOT flag mild, constructive, or polite policy suggestions (e.g., "I hope they improve road repairs soon" or "It would be nice if the application process were faster").
- DO NOT flag neutral questions about rules or procedures (e.g., "When does the new tax rule take effect?").

LANGUAGE & SLANG AWARENESS:
- Comments may be written in any language (including Azerbaijani, Turkish, Russian, English, mixed code-switching, transliterated text, or local slang/sarcasm).
- Always write the `reason` field in clear, concise English (strictly 1 sentence).

OUTPUT FORMAT:
Return a valid JSON object matching this exact schema:
{
  "results": [
    {
      "id": "<comment_batch_id>",
      "on_topic": true | false,
      "severely_negative": true | false,
      "severity_score": 0-10,
      "reason": "1-sentence explanation if severely_negative is true, otherwise null"
    }
  ]
}

Scoring Rubric:
- 0-3: Off-topic, neutral, positive, or trivial chatter.
- 4-6: Mild criticism, frustration without hostility, or polite disagreement (severely_negative = false).
- 7-8: Strong hostility, harsh condemnation, or explicit opposition to government policies/institutions (severely_negative = true).
- 9-10: Extreme hostility, vitriolic attacks, calls for defiance/unrest, or severe anti-government rhetoric (severely_negative = true).
```

---

## 8. Telegram User History Lookup Engine

When the operator runs `/lookup <@username_or_id> [mode: all | negative]` inside the Telegram Bot, the system executes a **Two-Tier Hybrid Search**:

```mermaid
sequenceDiagram
    actor Op as Operator (Telegram Bot)
    participant Bot as Bot Controller (Aiogram)
    participant DB as PostgreSQL
    participant MT as Telethon Userbot Worker
    participant DS as DeepSeek API

    Op->>Bot: /lookup @target_user negative
    Bot->>MT: 1. Resolve @target_user -> numeric user_id + profile metadata
    Bot->>DB: 2. Query already-indexed comments for external_user_id
    Bot->>MT: 3. Discover all mutual chats via GetCommonChatsRequest(user_id)<br/>+ intersect/union with all Userbot joined groups/discussion chats
    loop For each mutual group/channel (Rate-Limit Safe)
        MT->>MT: iter_messages(chat, from_user=user_id, limit=500)
        MT->>DB: Upsert newly discovered historical messages
    end
    alt Mode == "negative" and newly discovered messages exist
        MT->>DS: Batch-evaluate unevaluated messages from this user
        DS-->>DB: Save severity_score & llm_reason
    end
    DB-->>Bot: Return complete matching comment list (sorted newest first)
    Bot-->>Op: Send paginated summary + downloadable HTML/CSV report if >20 messages
```

### Key Technical Details for User History Lookup
1. **Resolving Username or Numeric ID:**
   - If input starts with `@` or letters: calls `await client.get_entity(username)` to obtain `user.id`, `user.username`, `user.first_name`, `user.last_name`.
   - If input is numeric (`123456789`): queries `platform_authors` in PostgreSQL first (in case the user has no public username and Telethon hasn't cached their access hash), or calls `await client.get_entity(PeerUser(int(user_id)))`.
2. **Mutual Chat Discovery:**
   - Calls `await client(GetCommonChatsRequest(user_id=input_user, max_id=0, limit=100))` which directly asks Telegram's servers for every group/supergroup shared between the monitoring userbot account and the target user!
   - Also scans any monitored channel discussion boards where the user may have commented without formally joining the group.
3. **Server-Side Filtered Message Retrieval:**
   - Uses `async for msg in client.iter_messages(chat_entity, from_user=target_user, limit=1000):` — Telegram executes `messages.Search` server-side filtered by `from_id`, meaning it retrieves **only that user's messages** in seconds without downloading the entire group history.

---

## 9. Centralized Telegram Reporting & Bot Commands

### 9.1 Standardized Alert Payload Format
Whenever `is_severely_negative == True` (and `severity_score >= threshold`, default `7`), the Bot sends an HTML-formatted message (with `disable_web_page_preview=True` so alerts stay compact):

```html
🚨 <b>SEVERE NEGATIVE COMMENT DETECTED</b>
━━━━━━━━━━━━━━━━━━━━
🌐 <b>Platform:</b> {Platform Icon} {Platform Name}
📢 <b>Source:</b> {Channel / Group / Page Name}
🔥 <b>Severity:</b> {severity_score}/10

👤 <b>Author:</b> {Display Name} ({@username or "No Username"})
🆔 <b>User ID:</b> <code>{external_user_id}</code>

💬 <b>Comment:</b>
<blockquote>{Exact Comment Text (escaped HTML)}</blockquote>

🧠 <b>AI Assessment:</b>
<i>{1-sentence DeepSeek reason}</i>

🔗 <a href="{post_url}">View Post</a>  |  🎯 <a href="{comment_url}">Jump to Exact Comment</a>
🕒 <b>Time:</b> {YYYY-MM-DD HH:MM:SS UTC}
```

Each alert also includes an **Inline Keyboard**:
- `[🔍 User History]` (Immediately triggers `/lookup` if platform is Telegram, or queries local DB for other platforms)
- `[✅ Mark False Positive]` (Marks comment in DB as false positive for audit)

### 9.2 Bot Command Specification
Only Telegram User IDs listed in `ADMIN_TELEGRAM_IDS` environment variable can interact with the bot.
- `/start` & `/help` — Interactive dashboard showing active targets, queue depth, scraper session health, and 24h flagged comment count.
- `/add_target <url_or_tg_username>` — Automatically detects platform from URL (`t.me`, `youtube.com`/`youtu.be`, `instagram.com`, `facebook.com`/`fb.watch`, `x.com`/`twitter.com`), registers the target, and immediately triggers an initial scrape job.
- `/targets` — Paginated interactive list of monitored targets with `[⏸ Pause]`, `[🔄 Scrape Now]`, and `[🗑 Delete]` buttons.
- `/lookup <@username_or_id> [all|negative]` — Executes the Telegram User History Lookup across all mutual groups & channels; returns paginated interactive cards in chat plus a full downloadable `.html` / `.csv` report file when results exceed 10 messages.
- `/sessions` — Manage burner cookies/sessions and proxies for IG, FB, and X, plus view status of the Telegram MTProto userbot.
- `/stats` — Token usage, DeepSeek cache hit rate, comments processed vs. flagged per platform.

---

## 10. Coolify Deployment & Infrastructure Blueprint

The entire system is designed to be deployed as a single **Docker Compose** resource in Coolify.

### 10.1 Service Topology (`docker-compose.yml`)
1. **`postgres` (`postgres:16-alpine`):** Persistent volume `pg_data:/var/lib/postgresql/data` with healthcheck `pg_isready`.
2. **`redis` (`redis:7-alpine`):** Persistent volume `redis_data:/data` with AOF enabled.
3. **`migrator`:** Runs `alembic upgrade head` on startup and exits `0` before application services start.
4. **`bot-controller`:** Runs the Aiogram 3 Telegram Bot and the rate-limited Alert Dispatcher (25 messages/sec global cap, 1 msg/sec per chat cap to prevent `FloodWaitError`).
5. **`telegram-collector`:** Runs the persistent Telethon MTProto userbot event loop and listens to Redis RPC calls for on-demand `/lookup` deep scans. Uses persistent volume `telethon_sessions:/app/sessions`.
6. **`scraper-worker`:** Runs Playwright + Arq worker executing scheduled and on-demand comment scraping jobs for YouTube, X, Instagram, and Facebook. Configured with `shm_size: '2gb'` so Chromium never crashes on heavy pages.
7. **`llm-worker`:** Runs the Micro-Batch Aggregator and DeepSeek API classification pipeline.

### 10.2 Required Environment Variables (`.env.example`)
```ini
# Core Security & Access
ADMIN_TELEGRAM_IDS=123456789,987654321
SESSION_ENCRYPTION_KEY=<32-byte-fernet-key>

# Telegram Bot (Control & Alert Plane)
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# Telegram MTProto Userbot (Monitoring & Lookup Plane)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION_NAME=monitor_userbot

# DeepSeek LLM Configuration
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
LLM_BATCH_SIZE=40
LLM_BATCH_FLUSH_SECONDS=4
LLM_ALERT_SEVERITY_THRESHOLD=7

# YouTube Official API (Optional - falls back to InnerTube if empty)
YOUTUBE_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# Database & Redis (Auto-wired inside Docker Compose)
DATABASE_URL=postgresql+asyncpg://operatives:strongpassword@postgres:5432/operatives_db
REDIS_URL=redis://redis:6379/0
```

---

## 11. Anti-Detection, Rate-Limiting & Operational Safety Rules

1. **Telegram MTProto Safety (Preventing Account Freeze):**
   - Never use a newly registered VoIP Telegram number for the userbot; use an aged account and authenticate via QR code or session string generation script before deployment.
   - During `/lookup` deep scans across mutual groups, enforce a `0.8s–1.5s` delay between consecutive `iter_messages` chat queries and automatically honor `Telethon.errors.FloodWaitError(seconds)` if `seconds < 60`.
   - The userbot is strictly **read-only** in third-party groups (never sends messages, never reacts, never joins >5 groups per hour).
2. **IG / FB / X Burner Session Protection:**
   - Never login with username/password on every scrape run. Authenticate once, persist `storage_state` cookies encrypted in `scraper_sessions`, and reuse cookies until expired.
   - Always pair each burner account with a consistent proxy IP (`proxy_url`) and consistent `User-Agent` to prevent suspicious-login checkpoints.
   - Implement exponential jitter (`random.uniform(1.5, 4.0)` seconds) between cursor pagination requests when scraping 5,000+ comment posts.
3. **Alert Storm Prevention:**
   - If a 6,000-comment post yields 150 severely negative comments in one scrape batch, do not spam 150 messages in 1 second (which triggers Telegram Bot API 429). The `AlertDispatcher` streams individual alerts at 1 message per 1.5 seconds OR groups bursts >15 alerts from the same post into clean batch digests if configured.
