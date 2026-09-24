import os
import asyncio
import logging
import mimetypes
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn

from fastapi import FastAPI, Header, HTTPException
from telethon import TelegramClient, events
from telethon.sessions import StringSession


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

N8N_WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

PORT = int(os.environ.get("PORT", "8080"))

TEMP_DIR = Path(
    os.environ.get(
        "TEMP_DIR",
        "/tmp/telegram_videos"
    )
)

FILE_TTL_HOURS = int(
    os.environ.get("FILE_TTL_HOURS", "6")
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True
)


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


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "telegram-video-monitor"
    }


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


# ============================================================
# TELEGRAM
# ============================================================

client = TelegramClient(
    StringSession(TELEGRAM_SESSION),
    API_ID,
    API_HASH
)


# Prevent processing the same message twice
processing_messages = set()


# ============================================================
# HELPERS
# ============================================================

def is_video(message):
    """
    Check whether Telegram message contains a video.
    """

    if message.video:
        return True

    if message.document:

        mime_type = (
            message.document.mime_type
            or ""
        )

        return mime_type.startswith("video/")

    return False


def get_filename(message, message_id):

    if message.document:

        for attribute in message.document.attributes:

            if hasattr(attribute, "file_name"):

                if attribute.file_name:

                    return attribute.file_name

    return f"telegram_video_{message_id}.mp4"


def safe_filename(filename):

    filename = os.path.basename(
        filename
    )

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


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_old_files():

    while True:

        try:

            now = datetime.now(
                timezone.utc
            )

            max_age = timedelta(
                hours=FILE_TTL_HOURS
            )

            for file in TEMP_DIR.iterdir():

                if not file.is_file():
                    continue

                modified = datetime.fromtimestamp(
                    file.stat().st_mtime,
                    timezone.utc
                )

                if now - modified > max_age:

                    logger.info(
                        f"Deleting expired file: {file.name}"
                    )

                    try:
                        file.unlink()

                    except Exception as e:

                        logger.error(
                            f"Delete error: {e}"
                        )

        except Exception as e:

            logger.error(
                f"Cleanup error: {e}"
            )

        await asyncio.sleep(1800)


# ============================================================
# DELETE FILE AFTER N8N FINISHES
# ============================================================

@app.post("/complete/{filename}")
async def complete_video(
    filename: str,
    x_webhook_secret: str | None = Header(
        default=None
    )
):

    if x_webhook_secret != WEBHOOK_SECRET:

        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    filename = safe_filename(
        filename
    )

    file_path = TEMP_DIR / filename

    if not file_path.exists():

        return {
            "status": "already_deleted"
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

        logger.exception(
            "Could not delete file"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# SEND VIDEO TO N8N
# ============================================================

async def send_to_n8n(
    file_path,
    message,
    channel_username,
    channel_title
):

    filename = file_path.name

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

        "caption": (
            message.message
            or ""
        ),

        "channel_username": (
            channel_username
            or ""
        ),

        "channel_title": (
            channel_title
            or ""
        )
    }

    headers = {

        "X-Webhook-Secret":
            WEBHOOK_SECRET
    }

    timeout = httpx.Timeout(
        connect=30,
        read=900,
        write=900,
        pool=30
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

    return response


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

    message_id = message.id

    logger.info(
        f"New Telegram message: {message_id}"
    )

    # ----------------------------------------
    # Prevent duplicates
    # ----------------------------------------

    if message_id in processing_messages:

        logger.info(
            f"Already processing {message_id}"
        )

        return

    # ----------------------------------------
    # Ignore non-video posts
    # ----------------------------------------

    if not is_video(message):

        logger.info(
            f"Message {message_id} is not a video"
        )

        return

    processing_messages.add(
        message_id
    )

    file_path = None

    try:

        # ------------------------------------
        # Telegram info
        # ------------------------------------

        chat = await event.get_chat()

        channel_username = getattr(
            chat,
            "username",
            None
        )

        channel_title = getattr(
            chat,
            "title",
            ""
        )

        # ------------------------------------
        # Filename
        # ------------------------------------

        filename = safe_filename(
            get_filename(
                message,
                message_id
            )
        )

        filename = (
            f"{message_id}_{filename}"
        )

        file_path = TEMP_DIR / filename

        # ------------------------------------
        # Download
        # ------------------------------------

        logger.info(
            f"Downloading video {message_id}..."
        )

        downloaded = await client.download_media(
            message,
            file=str(file_path)
        )

        if not downloaded:

            logger.error(
                f"Download failed: {message_id}"
            )

            return

        logger.info(
            f"Downloaded: {file_path}"
        )

        # ------------------------------------
        # Send to n8n
        # ------------------------------------

        logger.info(
            f"Sending {filename} to n8n..."
        )

        response = await send_to_n8n(
            file_path,
            message,
            channel_username,
            channel_title
        )

        logger.info(
            f"n8n HTTP status: {response.status_code}"
        )

        if response.status_code >= 400:

            logger.error(
                f"n8n rejected video: "
                f"{response.text[:1000]}"
            )

            # Keep file.
            # Cleanup will remove it later.

            return

        logger.info(
            f"Video accepted by n8n: {filename}"
        )

        # IMPORTANT:
        # DO NOT DELETE HERE.
        #
        # n8n will call:
        #
        # POST /complete/{filename}
        #
        # after publishing finishes.

    except Exception as e:

        logger.exception(
            f"Processing error for {message_id}: {e}"
        )

    finally:

        processing_messages.discard(
            message_id
        )


# ============================================================
# TELEGRAM WORKER
# ============================================================

async def telegram_worker():

    logger.info(
        "Connecting to Telegram..."
    )

    await client.start()

    me = await client.get_me()

    logger.info(
        "Telegram login successful"
    )

    logger.info(
        f"Account: "
        f"{getattr(me, 'username', None) or me.id}"
    )

    # Check channel access
    try:

        entity = await client.get_entity(
            TELEGRAM_CHANNEL
        )

        logger.info(
            f"Channel found: "
            f"{getattr(entity, 'title', TELEGRAM_CHANNEL)}"
        )

    except Exception as e:

        logger.error(
            f"Cannot access channel "
            f"{TELEGRAM_CHANNEL}: {e}"
        )

        raise

    logger.info(
        f"Monitoring: {TELEGRAM_CHANNEL}"
    )

    await client.run_until_disconnected()


# ============================================================
# FASTAPI SERVER
# ============================================================

async def api_server():

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

    server = uvicorn.Server(
        config
    )

    await server.serve()


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting Telegram Video Monitor..."
    )

    await asyncio.gather(

        telegram_worker(),

        api_server(),

        cleanup_old_files()
    )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Service stopped"
        )
