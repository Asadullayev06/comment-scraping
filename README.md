# Multi-Platform Negative Comment Monitoring & Reporting System

A centralized monitoring and alerting system controlled via a Telegram Bot. The system tracks public comments across five social media platforms (**Telegram**, **Instagram**, **Facebook**, **X/Twitter**, and **YouTube**), filters them using DeepSeek AI to detect severe hostility and negativity against government policies/regulations, and immediately alerts administrators inside Telegram with rich metadata and deep links.

## System Highlights
- **Telegram MTProto Userbot**: Reads public and private groups/channel discussions without admin privileges.
- **DeepSeek V3 Integration**: 40-comment micro-batching + static prefix context caching.
- **On-Demand User Lookup**: Fast search across mutual groups for any Telegram user.
- **Coolify Deployment Ready**: Complete multi-container Docker Compose setup.
