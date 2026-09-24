import os
import asyncio
import logging
import mimetypes
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Header
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import uvicorn


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

N8N_WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

TEMP_DIR = Path(
    os.environ.get("TEMP_DIR", "/tmp/telegram_videos")
)

FILE_TTL_HOURS = int(
    os.environ.get("FILE_TTL_HOURS", "6")
)

TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("telegram-monitor")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Telegram Video Monitor"
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "telegram-video-monitor"
    }


# ============================================================
# TELEGRAM CLIENT
# ============================================================

client = TelegramClient(
    StringSession(TELEGRAM_SESSION),
    API_ID,
    API_HASH
)


# ============================================================
# HELPERS
# ============================================================

def is_video(message) -> bool:
    """
    Detect normal Telegram videos and video documents.
    """

    if message.video:
        return True

    if message.document:
        mime = message.document.mime_type or ""

        if mime.startswith("video/"):
            return True

    return False


def get_filename(message, message_id: int) -> str:
    """
    Try to get the original filename.
    """

    if message.document:
        for attribute in message.document.attributes:

            # Telegram DocumentAttributeFilename
            if hasattr(attribute, "file_name"):
                if attribute.file_name:
                    return attribute.file_name

    return f"telegram_video_{message_id}.mp4"


def safe_filename(filename: str) -> str:
    """
    Prevent weird paths / characters.
    """

    filename = os.path.basename(filename)

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "._- "
    )

    filename = "".join(
        c if c in allowed else "_"
        for c in filename
    )

    return filename[:150]


async def cleanup_old_files():
    """
    Delete files older than FILE_TTL_HOURS.
    This is a safety net in case n8n crashes.
    """

    while True:

        try:

            now = datetime.now(timezone.utc)

            expiration = timedelta(
                hours=FILE_TTL_HOURS
            )

            for file in TEMP_DIR.iterdir():

                if not file.is_file():
                    continue

                modified = datetime.fromtimestamp(
                    file.stat().st_mtime,
                    timezone.utc
                )

                if now - modified > expiration:

                    logger.info(
                        f"Deleting expired file: {file}"
                    )

                    try:
                        file.unlink()
                    except Exception as e:
                        logger.error(
                            f"Could not delete {file}: {e}"
                        )

        except Exception as e:

            logger.error(
                f"Cleanup error: {e}"
            )

        await asyncio.sleep(1800)  # every 30 minutes


# ============================================================
# COMPLETE CALLBACK
# ============================================================

@app.post("/complete/{filename}")
async def complete_video(
    filename: str,
    x_webhook_secret: str | None = Header(default=None)
):

    if x_webhook_secret != WEBHOOK_SECRET:

        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    filename = safe_filename(filename)

    file_path = TEMP_DIR / filename

    if not file_path.exists():

        return {
            "status": "already_deleted",
            "filename": filename
        }

    try:

        file_path.unlink()

        logger.info(
            f"Deleted completed video: {filename}"
        )

        return {
            "status": "deleted",
            "filename": filename
        }

    except Exception as e:

        logger.error(
            f"Delete failed: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail="Could not delete file"
        )


# ============================================================
# TELEGRAM NEW MESSAGE
# ============================================================

@client.on(
    events.NewMessage(
        chats=TELEGRAM_CHANNEL
    )
)
async def new_message_handler(event):

    message = event.message

    logger.info(
        f"New Telegram message: {message.id}"
    )

    # Ignore messages without video
    if not is_video(message):

        logger.info(
            f"Message {message.id} has no video. Ignored."
        )

        return

    filename = safe_filename(
        get_filename(
            message,
            message.id
        )
    )

    # Make filename unique
    filename = (
        f"{message.id}_{filename}"
    )

    file_path = TEMP_DIR / filename

    logger.info(
        f"Downloading video: {filename}"
    )

    try:

        downloaded_path = await client.download_media(
            message,
            file=str(file_path)
        )

        if not downloaded_path:

            logger.error(
                f"Failed to download video {message.id}"
            )

            return

        logger.info(
            f"Downloaded: {file_path}"
        )

    except Exception as e:

        logger.exception(
            f"Telegram download error: {e}"
        )

        return

    # --------------------------------------------------------
    # Caption
    # --------------------------------------------------------

    caption = message.message or ""

    # --------------------------------------------------------
    # Telegram channel information
    # --------------------------------------------------------

    chat = await event.get_chat()

    username = getattr(
        chat,
        "username",
        None
    )

    channel_title = getattr(
        chat,
        "title",
        ""
    )

    # --------------------------------------------------------
    # Send to n8n
    # --------------------------------------------------------

    try:

        logger.info(
            f"Sending {filename} to n8n..."
        )

        mime_type = (
            mimetypes.guess_type(
                str(file_path)
            )[0]
            or "video/mp4"
        )

        data = {

            "message_id": str(
                message.id
            ),

            "filename": filename,

            "caption": caption,

            "channel_username": (
                username or ""
            ),

            "channel_title": (
                channel_title or ""
            ),

            "telegram_message_id": str(
                message.id
            )
        }

        headers = {

            "X-Webhook-Secret":
                WEBHOOK_SECRET
        }

        timeout = httpx.Timeout(
            connect=30.0,
            read=900.0,
            write=900.0,
            pool=30.0
        )

        async with httpx.AsyncClient(
            timeout=timeout
        ) as http:

            with open(
                file_path,
                "rb"
            ) as video_file:

                files = {

                    "video": (
                        filename,
                        video_file,
                        mime_type
                    )
                }

                response = await http.post(
                    N8N_WEBHOOK_URL,
                    data=data,
                    files=files,
                    headers=headers
                )

        logger.info(
            f"n8n response: {response.status_code}"
        )

        if response.status_code >= 400:

            logger.error(
                f"n8n error: {response.text}"
            )

            # DO NOT DELETE.
            # Cleanup process will remove it later.

            return

        logger.info(
            f"Video successfully accepted by n8n: {filename}"
        )

    except Exception as e:

        logger.exception(
            f"Failed sending video to n8n: {e}"
        )

        # DO NOT DELETE.
        # Keep file for retry/manual recovery.


# ============================================================
# STARTUP
# ============================================================

async def telegram_worker():

    logger.info(
        "Starting Telegram client..."
    )

    await client.start()

    me = await client.get_me()

    logger.info(
        f"Telegram logged in as: "
        f"{getattr(me, 'username', None) or me.id}"
    )

    logger.info(
        f"Monitoring channel: {TELEGRAM_CHANNEL}"
    )

    await client.run_until_disconnected()


async def main():

    cleanup_task = asyncio.create_task(
        cleanup_old_files()
    )

    telegram_task = asyncio.create_task(
        telegram_worker()
    )

    await telegram_task

    cleanup_task.cancel()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
