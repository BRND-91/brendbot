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
    claude_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_MODEL", "sonnet")
    )
    discord_bot_id: str = field(
        default_factory=lambda: os.getenv("DISCORD_BOT_ID", "")
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
