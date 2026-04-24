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
    trusted_discord_ids: set[str] = field(default_factory=set)
    # Manual friend-tier override. Comma-separated guild snowflakes
    # from ``FRIEND_GUILD_IDS``. Any guild listed here is classified
    # as friend-tier unconditionally, bypassing the owner-match and
    # member-cache checks. Pragmatic escape hatch for cases where the
    # automatic classifier can't see the signal — e.g. the admin
    # isn't the Discord owner of the server (someone else created it)
    # AND the MEMBERS privileged intent isn't enabled, so the
    # member-cache fallback is also empty at ``on_ready``. Pilot
    # 2026-04-24 revealed Pizzacord in exactly this shape.
    friend_guild_ids: set[str] = field(default_factory=set)
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
        raw_friend = os.getenv("FRIEND_GUILD_IDS", "")
        if raw_friend:
            self.friend_guild_ids = {
                x.strip() for x in raw_friend.split(",") if x.strip()
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


# ── Friend-tier guild classification ─────────────────────────────────────
#
# A "friend-tier" guild is a private owner-occupied server small enough
# to treat as a trust circle: no content gate, no haiku prefilter, no
# FLAG reroute. The classification is auto-detected at bot startup (see
# discord.classify_friend_guilds) rather than opt-in via env var,
# because the opt-in model from PR #20 failed silently when the operator
# didn't know to flip the switch.
#
# Classification rule: admin is the guild owner AND member_count < 25.
# Both conditions are required — a giant community server the admin
# happens to own isn't friend-tier, and a small server where the admin
# is a member but not the owner isn't either. Conservative defaults:
# unknown member_count (Discord didn't populate it) → not friend-tier.
#
# DMs with the admin are also friend-tier; they hit this via empty
# guild_id, which is_friend_guild accepts explicitly.

_FRIEND_GUILDS: frozenset[str] = frozenset()


def set_friend_guilds(guild_ids: frozenset[str]) -> None:
    """Replace the classified friend-guild set. Called from
    ``discord.classify_friend_guilds`` at bot startup. Tests can call
    this directly to stub the classification."""
    global _FRIEND_GUILDS
    _FRIEND_GUILDS = guild_ids


def get_friend_guilds() -> frozenset[str]:
    """Return the current classified friend-guild set."""
    return _FRIEND_GUILDS


def is_friend_guild(guild_id: str) -> bool:
    """Return True if ``guild_id`` is classified friend-tier.

    Empty ``guild_id`` (DM) counts as friend-tier only when the DM
    partner is the admin; callers that need that semantic should check
    sender tier explicitly. This helper just handles the guild-level
    classification and returns False on empty guild_id so the safe
    default is "apply gates."
    """
    if not guild_id:
        return False
    return guild_id in _FRIEND_GUILDS
