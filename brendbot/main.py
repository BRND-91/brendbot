"""brendbot — entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from brendbot.config import get_config
from brendbot.discord import DiscordListener
from brendbot.session import SessionPool, warm_classifier_pool

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

    async def on_text(chat_id: str, text: str) -> str | None:
        """Send Claude's text response back to Discord. Returns the first
        chunk's message ID so the session layer can log it for feedback
        correlation, or None on send failure."""
        from brendbot.discord import send_message
        try:
            return await send_message(chat_id, text)
        except Exception:
            logger.exception("Failed to send text response to %s", chat_id)
            return None

    async def on_text_edit(chat_id: str, message_id: str, text: str) -> bool:
        """Edit an existing Discord message in-place for streaming."""
        from brendbot.discord import edit_message
        try:
            return await edit_message(chat_id, message_id, text)
        except Exception:
            logger.debug("Failed to edit streaming message %s", message_id)
            return False

    pool = SessionPool(
        model=cfg.claude_model,
        bot_name=cfg.bot_name,
        on_text=on_text,
        on_text_edit=on_text_edit,
        max_sessions=cfg.max_sessions,
    )

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
        address_level: str = "high",
        score: float | None = None,
        haiku_invoked: bool = False,
    ) -> None:
        tier = cfg.tier_for(sender_id)
        is_group = platform == "discord"  # discord = guild, discord_dm = DM

        logger.info("Routing message from %s (tier=%s, is_group=%s, domains=%s, addr=%s)", sender_id, tier, is_group, domain_hint or "none", address_level)
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
                address_level=address_level,
                score=score,
                haiku_invoked=haiku_invoked,
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

    def _refresh_caches() -> None:
        # All cached config is now SIGHUP-refreshable: soul files,
        # FUSED-CORE, MANIFEST, and engagement.yaml (scoring deltas,
        # thresholds, domains, classifier prompts, content gate config).
        # Use `kill -HUP <pid>` to trigger.
        logger.info("SIGHUP received — refreshing all caches")
        try:
            pool.refresh_cache()
        except Exception:
            logger.exception("Cache refresh failed")

    try:
        loop.add_signal_handler(signal.SIGHUP, _refresh_caches)
    except (AttributeError, NotImplementedError):
        # SIGHUP not available on Windows; skip silently.
        pass

    logger.info("=" * 50)
    logger.info("brendbot STARTED (model=%s)", cfg.claude_model)
    logger.info("=" * 50)

    # ── Boot-split: warm classifier pool concurrently with gateway ────
    # The Discord gateway handshake (IDENTIFY, READY, guild sync) and the
    # classifier pool warm-up (spawning 3 haiku subprocesses) are
    # independent I/O-bound operations. Running them concurrently cuts
    # ~15-18s off time-to-first-response vs sequential startup.
    warm_task = asyncio.create_task(
        warm_classifier_pool(), name="classifier-pool-warmup"
    )

    try:
        await listener.run()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("brendbot shutting down...")
        # Cancel warm-up if it's still running at shutdown
        if not warm_task.done():
            warm_task.cancel()
            try:
                await warm_task
            except (asyncio.CancelledError, Exception):
                pass
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
