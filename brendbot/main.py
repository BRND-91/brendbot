"""brendbot — entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from brendbot.config import get_config
from brendbot.discord import DiscordListener
from brendbot.session import SessionPool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run() -> None:
    cfg = get_config()

    if not cfg.discord_token:
        logger.error("DISCORD_TOKEN not set! Add it to your .env file.")
        sys.exit(1)

    async def on_text(chat_id: str, text: str) -> None:
        """Send Claude's text response back to Discord."""
        from brendbot.discord import send_message
        try:
            await send_message(chat_id, text)
        except Exception:
            logger.exception("Failed to send text response to %s", chat_id)

    pool = SessionPool(model=cfg.claude_model, bot_name=cfg.bot_name, on_text=on_text)

    async def on_message(
        platform: str,
        sender_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
        reply_to_id: str = "",
        reply_to_text: str = "",
        reply_to_author: str = "",
        context_messages: list | None = None,
        is_direct_mention: bool = False,
        domain_hint: str = "",
    ) -> None:
        tier = cfg.tier_for(sender_id)
        is_group = platform == "discord"  # discord = guild, discord_dm = DM

        logger.info("Routing message from %s (tier=%s, is_group=%s, domains=%s)", sender_id, tier, is_group, domain_hint or "none")
        try:
            await pool.route_message(
                platform=platform,
                sender_id=sender_id,
                chat_id=chat_id,
                text=text,
                tier=tier,
                is_group=is_group,
                message_id=message_id,
                reply_to_id=reply_to_id,
                reply_to_text=reply_to_text,
                reply_to_author=reply_to_author,
                context_messages=context_messages,
                is_direct_mention=is_direct_mention,
                domain_hint=domain_hint,
            )
        except Exception:
            logger.exception("Error routing message from %s", sender_id)

    listener = DiscordListener(token=cfg.discord_token, on_message=on_message)

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(signal.SIGINT, _shutdown)
    loop.add_signal_handler(signal.SIGTERM, _shutdown)

    logger.info("=" * 50)
    logger.info("brendbot STARTED (model=%s)", cfg.claude_model)
    logger.info("=" * 50)

    try:
        await listener.run()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("brendbot shutting down...")
        await pool.stop_all()
        import gc
        gc.collect()
        logger.info("brendbot stopped.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
