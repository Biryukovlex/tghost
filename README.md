# TGhost

[![TGhost — Your channel’s history. Your next great post.](docs/brand/readme-banner.svg)](https://tghost.app/)

[Website](https://tghost.app/) · [Quick start](#quick-start-with-docker) · [Documentation](#documentation)

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Telegram analytics, an AI writing desk and a publishing calendar in one
self-hosted workspace.

## What you can do

- **Overview:** track views, reactions, comments and shares; explore stored
  posts, read their discussion comments and export CSVs.
- **Studio:** research ideas, write and edit drafts, attach images and keep
  conversation and draft history. Channel profiles use the top 30 posts,
  ranked by code before they reach the model. Add other owned channels through
  **References**.
- **Calendar:** schedule text, photos or albums in Telegram; review, reschedule
  and cancel posts in month, week or list views. Publishing always requires
  your confirmation and the connected account's posting permission. Once
  Telegram accepts a schedule, it can publish while TGhost is offline.
- **Settings:** manage channels, Telegram connection, model provider and
  optional web research; inspect agent runs and failures.

**Current scope:** a private, single-owner installation. Public hosting with
separate user accounts is not supported yet; authentication and account
isolation need further work before a public launch.

## Product tour

Illustrative demos from [tghost.app](https://tghost.app/), using a fictional
channel and sample content.

### Overview

![Illustrative TGhost Overview: subscribers, typical views, ER, ERR and channel trends](docs/screenshots/tghost-overview.png)

### AI Studio

![Illustrative TGhost AI Studio: channel conversations, post analysis and a versioned draft workspace](docs/screenshots/tghost-studio.png)

### Calendar

![Illustrative TGhost Calendar: month view with published and scheduled posts for a fictional channel](docs/screenshots/tghost-calendar.png)

## Quick start with Docker

Requires Docker Engine with Compose v2, or Docker Desktop.

### 1. Get the app

```bash
git clone https://github.com/Biryukovlex/tghost.git
cd tghost
cp .env.example .env
```

### 2. Configure private settings

Edit `.env`:

| Setting | Value |
| --- | --- |
| `ADMIN_PASSWORD` | A strong password for the web panel; replace `change-me`. |
| `POSTGRES_PASSWORD` | A strong database password; replace `change-me`. |
| `DATABASE_URL` | `postgresql+asyncpg://tg_stats:<database-password>@postgres:5432/tg_stats` using the same password, URL-encoded if necessary. |
| `TELEGRAM_SESSION_ENCRYPTION_KEY` | Generate with the command below, then paste the result here. |

Build the image and generate the encryption key locally:

```bash
docker compose build tg-studio
docker compose run --rm --no-deps tg-studio python scripts/generate_session_key.py
```

Keep `.env` and the generated key private. Back up the key with your database;
it is required to decrypt saved connections and credentials.

### 3. Start the app

```bash
docker compose up -d postgres
docker compose --profile migrate run --rm migrate
docker compose up -d tg-studio
```

Open [localhost:8080](http://127.0.0.1:8080) and sign in with
`ADMIN_USERNAME` (default: `admin`) and your password.

The panel starts without a Telegram connection. To enable collection:

1. Get your API ID and hash from [my.telegram.org](https://my.telegram.org).
2. Create a Telethon user session with `scripts/generate_session.py` as
   described in the [setup guide](docs/deployment.md#first-local-docker-setup).
3. Save the connection and add channels in **Settings → Telegram**.
4. Restart the app: `docker compose restart tg-studio`.

The Telegram account must be able to read each channel. Scheduling additionally
requires permission to publish there. TGhost uses a user session, not a bot.

### Update an existing installation

Back up PostgreSQL and your encryption key first; follow the
[backup and restore guide](docs/deployment.md#backups-and-restore).
For the default Docker setup:

```bash
git pull --ff-only
docker compose --profile migrate build tg-studio migrate
docker compose stop tg-studio
docker compose --profile migrate run --rm migrate
docker compose up -d tg-studio
```

For separate web and worker processes, use the
[split deployment guide](docs/deployment.md#explicit-process-roles).

## Choose your model

Open **Settings → Studio**, configure a provider, select a model and save.

| Provider | Connection |
| --- | --- |
| **OpenRouter** | Server-side API key and model selection. |
| **Ollama** | Installed local models with tool calling. Default: `http://127.0.0.1:11434`; Docker connects through `host.docker.internal`. Linux may require a host-gateway mapping. |
| **OpenAI / ChatGPT** | **Continue with ChatGPT**, without an API key. Available models and limits depend on your account. Sign-in currently requires opening the app on `http://127.0.0.1:8080` on its host; a remote HTTPS site is not supported by this flow. |

External providers require consent before receiving channel context. Local
Ollama requests stay on the configured local server. Provider selection does
not change Telegram collection or scheduling.

For web research, connect the optional private SearXNG service using the
[research setup guide](docs/deployment.md#studio-and-web-research).

## Storage and deployment

PostgreSQL is the only database for the app and its maintenance tools. SQLite
import support has been removed. Telegram sessions and provider credentials
saved through Settings are encrypted; the encryption key stays outside the
database. Keep backups, Telegram sessions, provider keys and collected content
out of Git.

The default Docker configuration publishes the app and database on loopback
only. For private server access, use a VPN or SSH tunnel. Read the [deployment runbook](docs/deployment.md)
for migrations, backups, HTTPS configuration and separate web/worker processes.
Do not expose this release as a public service with shared administrator access.

Existing package, service and storage identifiers retain
`tg-studio` for compatibility with earlier installations.

## Documentation

- [Calendar, images and Telegram scheduling](docs/publishing.md)
- [Deployment, upgrades and backups](docs/deployment.md)
- [Privacy and data flow](docs/privacy.md)
- [Development and tests](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) and
[third-party license notes](docs/license-notices.md) for bundled assets and
optional services.
