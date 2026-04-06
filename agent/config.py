"""
AkashGuard configuration. All timing and threshold values can be overridden via .env.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Venice AI (voice/vision) ---
    venice_api_key: str = ""
    venice_api_base: str = "https://api.venice.ai/api/v1"
    venice_tts_model: str = "tts-kokoro"
    venice_tts_voice: str = "af_sky"
    venice_vision_model: str = "qwen3-235b-a22b-instruct-2507"
    venice_chat_model: str = "llama-3.3-70b"

    # --- Groq LLM (diagnosis) — OpenAI-compatible API ---
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"

    # --- AkashML LLM (diagnosis) — optional fallback; not used when Groq is configured above ---
    akashml_api_key: str = ""
    akashml_base_url: str = "https://api.akashml.com/v1"
    akashml_model: str = "meta-llama/Llama-3.3-70B-Instruct"

    # --- Akash Console API ---
    akash_console_api_key: str = ""
    akash_console_api_base: str = "https://console-api.akash.network/v1"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Langfuse ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # --- Database ---
    db_path: str = "./data/akashguard.db"

    # --- Health check ---
    health_check_interval: int = 15  # Seconds between monitor cycles
    failure_threshold: int = 3  # Consecutive failures before recovery is considered
    response_time_threshold_ms: int = 5000

    # --- Recovery timing (reduced for faster feedback) ---
    recovery_cooldown_seconds: int = 60  # Cooldown after successful recovery before re-evaluating
    recovery_bid_wait_seconds: int = 15  # Wait for provider bids after creating deployment
    recovery_uri_poll_seconds: int = 60  # Max time to poll for service URIs after accepting bid
    recovery_uri_poll_interval_seconds: int = 5  # Interval between URI poll attempts
    recovery_lease_retry_delay_seconds: int = 5  # Delay between create_lease retries
    # --- Recovery concurrency ---
    recovery_parallel: bool = False  # If True, allow up to recovery_parallel_max recoveries at once
    recovery_parallel_max: int = 2  # Max concurrent recoveries when recovery_parallel is True
    # --- Provider selection (avoid always same provider) ---
    recovery_bid_top_n: int = 3  # Pick randomly from top N cheapest bids for diversity (1 = always cheapest)
    recovery_failed_provider_avoid_seconds: int = 1800  # After a provider fails create_lease, avoid them for this long (0 = no expiry)

    # --- Agent ---
    agent_auto_monitor: bool = False  # When False, only dashboard/SSE; no monitoring loop

    # --- Auto-discovery ---
    auto_discover_deployments: bool = False
    auto_discover_interval_seconds: int = 30  # Min seconds between syncs with Akash Console
    auto_discover_sdl_template_path: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
