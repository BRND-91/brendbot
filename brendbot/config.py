"""Configuration from .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


@dataclass
class Config:
    discord_token: str = field(
        default_factory=lambda: os.getenv("DISCORD_TOKEN", "")
    )
    bot_name: str = field(
        default_factory=lambda: os.getenv("BOT_NAME", "brendbot")
    )
    admin_discord_id: str = field(
        default_factory=lambda: os.getenv("ADMIN_DISCORD_ID", "")
    )
    # Guild snowflake for the owner's own Discord server. When set, the
    # content gate's admin-bypass and the haiku prefilter are both
    # short-circuited on messages originating there — the assumption is
    # that a private owner-occupied server doesn't need the defensive
    # guards designed for public deployments. Unset → guards apply
    # uniformly across all guilds (the original behaviour).
    owner_guild_id: str = field(
        default_factory=lambda: os.getenv("OWNER_GUILD_ID", "")
    )
    trusted_discord_ids: set[str] = field(default_factory=set)
    claude_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_MODEL", "sonnet")
    )
    discord_bot_id: str = field(
        default_factory=lambda: os.getenv("DISCORD_BOT_ID", "")
    )
    # Optional Discord channel ID for operator alerts (classifier outages,
    # session-pool evictions). Empty disables Discord alerting; log files
    # under logs/ are unaffected either way.
    admin_alert_channel: str = field(
        default_factory=lambda: os.getenv("ADMIN_ALERT_CHANNEL", "")
    )
    # GCP project / region for Google Imagen via Vertex AI. Used by
    # scripts/generate-image. Credentials come from ADC (gcloud auth
    # application-default login) — these fields only parameterise which
    # project and region to call.
    gcp_project: str = field(
        default_factory=lambda: os.getenv("GCP_PROJECT", "")
    )
    gcp_location: str = field(
        default_factory=lambda: os.getenv("GCP_LOCATION", "us-central1")
    )
    imagen_model_default: str = field(
        default_factory=lambda: os.getenv("IMAGEN_MODEL", "imagen-4.0-generate-001")
    )
    # Content-gate FLAG reroute model. When set, overrides engagement.yaml's
    # flagged_path.model. The yaml used to hardcode a dated sonnet revision
    # (claude-sonnet-4-20250514) which ages badly — six months after deploy
    # the revision may be silently unreachable. This env var lets operators
    # pin a fresh model without editing yaml, and the yaml entry becomes
    # the fallback when the var is unset.
    claude_flagged_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_FLAGGED_MODEL", "")
    )
    # Session pool cap with LRU eviction (Stage 3). 0 disables the cap.
    max_sessions: int = field(
        default_factory=lambda: int(os.getenv("MAX_SESSIONS", "20"))
    )

    def __post_init__(self) -> None:
        raw = os.getenv("TRUSTED_DISCORD_IDS", "")
        if raw:
            self.trusted_discord_ids = {
                x.strip() for x in raw.split(",") if x.strip()
            }

    def tier_for(self, user_id: str) -> str:
        """Return the access tier for a Discord user."""
        if user_id == self.admin_discord_id:
            return "admin"
        if user_id in self.trusted_discord_ids:
            return "trusted"
        return "default"


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
