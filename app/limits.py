"""Fixed tuning constants formerly exposed as environment keys.

All values are application-owned bounds (timeouts, pool sizes, token budgets,
engine allow-lists). Operators do not need to change them per deployment.
Tests may monkeypatch module attributes at runtime.
"""

# Studio agent bounds
PROVIDER_TIMEOUT_SECONDS = 45.0  # HTTP timeout for the OpenRouter provider
RUN_TIMEOUT_SECONDS = 600.0  # end-to-end budget for a multi-tool agent run, including a post pipeline
RUN_CONCURRENCY = 2  # maximum concurrent agent runs per process
RUN_LEASE_SECONDS = 120  # durable PostgreSQL ownership lease
RUN_HEARTBEAT_SECONDS = 20.0  # renewal/cancellation polling interval
QUEUED_RUN_GRACE_SECONDS = 60  # age at which queued work is recovered as interrupted
TOOL_TIMEOUT_SECONDS = 30.0  # per-tool call timeout
TOOL_CONCURRENCY = 6  # maximum concurrent tool executions
MAX_TOOL_CALLS = 12  # agent tool-call budget per run
DELIVERABLE_TOOL_CALLS = 2  # extra calls reserved so a finalizing turn can still save the draft
MAX_OUTPUT_TOKENS = 16_384  # output per model request, including hidden reasoning
MAX_RUN_OUTPUT_TOKENS = 96_000  # safety cap on cumulative output across a run and its pipeline steps
CONTEXT_MAX_CHARS = 18_000  # maximum serialized evidence context for the agent
TOOL_RESULT_MAX_CHARS = 48_000  # complete model-facing research result
MAX_INPUT_TOKENS = 48_000  # estimated input per model request
MAX_RUN_INPUT_TOKENS = 200_000  # cumulative input per agent run
HISTORY_MAX_CHARS = 48_000  # recent completed chat messages
COMMENT_RECHECK_SECONDS = 1_800  # reconcile unchanged comment threads every 30 minutes
MAX_EVIDENCE_POSTS = 20  # bounded post evidence objects in a context pack
MIN_PROFILE_POSTS = 5  # minimum posts before a channel profile is considered ready

# Channel tools and post pipeline bounds
TOP_POSTS = 8  # top posts shown with full Markdown and used for the style brief
POST_TEXT_MAX_CHARS = 3_000  # Markdown characters per post returned to a model
CHANNEL_INDEX_PAGE = 20  # maximum posts per list_channel_posts page
NOVELTY_MAX_POSTS = 400  # most recent text posts/drafts compared for novelty and copying
PIPELINE_TIMEOUT_SECONDS = 420.0  # one write_post or revise_draft job
PIPELINE_STEP_TIMEOUT_SECONDS = 180.0  # one non-streamed model step; long posts take 60-90 s on free models
PIPELINE_MAX_REQUESTS = 16  # model requests one pipeline job may add to a run
PIPELINE_MAX_QUERIES = 8  # search queries planned per discovery
PIPELINE_MAX_LISTED = 30  # search results shown to the page picker
PIPELINE_MAX_PAGES = 6  # pages read per pipeline job
PIPELINE_PAGE_CHARS = 3_500  # page text characters given to story extraction
COPY_SHINGLE_WORDS = 8  # consecutive words that count as copied text

# Research and source reader bounds
SEARCH_PROVIDER = "searxng"  # only supported provider
SEARCH_TIMEOUT_SECONDS = 10.0  # SearXNG request timeout
SEARCH_RETRIES = 1  # retries on transient SearXNG failures
SEARCH_ENGINES = "yandex,github,arxiv,wikipedia"  # default engine mix
SEARCH_ALLOWED_ENGINES = "yandex,github,arxiv,wikipedia,bing,bing news,google,google news,brave,brave news,mojeek,qwant"  # catalog the agent may choose from
SEARCH_MAX_QUERIES = 12  # single clamp for query variants per search plan
SEARCH_MAX_RESULTS = 10  # max results per query
SEARCH_CACHE_TTL_SECONDS = 900  # TTL for normalized search-result cache
SOURCE_READER_ENABLED = True  # enable the SSRF-safe source reader
SOURCE_TIMEOUT_SECONDS = 10.0  # source fetch timeout
SOURCE_MAX_BYTES = 1_000_000  # hard byte limit for source reads
SOURCE_MAX_REDIRECTS = 3  # max redirects followed per source
SOURCE_MAX_CHARS = 12_000  # max extracted characters per source

# Provider and storage internals
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"  # OpenRouter endpoint (keeps consent fingerprint stable)
DATABASE_POOL_SIZE = 5  # SQLAlchemy pool size
DATABASE_MAX_OVERFLOW = 5  # pool overflow beyond size
DATABASE_POOL_TIMEOUT = 30.0  # seconds to wait for a pooled connection
DATABASE_POOL_RECYCLE = 1800  # connection recycle interval
WORKSPACE_SLUG = "community"  # single community workspace identifier
TELEGRAM_CONNECTION_LABEL = "default"  # single Telegram connection per workspace
FULL_RESCAN_HOURS = 24  # full history rescan interval in whole-history mode
