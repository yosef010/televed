import os
import asyncio
import logging
import mimetypes
import shutil
from pathlib import Path

import httpx
import uvicorn

from fastapi import FastAPI, Header, HTTPException
from telethon import TelegramClient, events
from telethon.sessions import StringSession


# =========================================================
# Environment Variables
# =========================================================

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

N8N_WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

PORT = int(os.environ.get("PORT", "8080"))

TEMP_DIR = Path(
    os.environ.get("TEMP_DIR", "/tmp/telegram_videos")
)

MAX_VIDEO_MB = int(
    os.environ.get("MAX_VIDEO_MB", "200")
)

MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("telegram-monitor")


# =========================================================
# FastAPI
# =========================================================

app = FastAPI(
    title="Telegram Video Monitor"
)


# =========================================================
# Telegram Client
# =========================================================

client = TelegramClient(
    StringSession(TELEGRAM_SESSION),
    API_ID,
    API_HASH
)


# =========================================================
# Processing State
# =========================================================

process_lock = asyncio.Lock()

processing_messages = set()


# =========================================================
# Filename Helpers
# =========================================================

def safe_filename(filename: str) -> str:
    """
    Make sure the filename is safe to use on the filesystem.
    """

    filename = os.path.basename(filename)

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789._- "
    )

    filename = "".join(
        char if char in allowed else "_"
        for char in filename
    )

    return filename[:150]


def get_filename(message, message_id: int) -> str:
    """
    Try to get the original Telegram filename.
    """

    if message.document:

        for attribute in message.document.attributes:

            if (
                hasattr(attribute, "file_name")
                and attribute.file_name
            ):
                return attribute.file_name

    return f"telegram_video_{message_id}.mp4"


# =========================================================
# Video Detection
# =========================================================

def is_video(message) -> bool:
    """
    Detect whether a Telegram message contains a video.
    """

    if message.video:
        return True

    if message.document:

        mime_type = (
            message.document.mime_type or ""
        )

        if mime_type.startswith("video/"):
            return True

    return False


# =========================================================
# Temporary Directory Cleanup
# =========================================================

def cleanup_temp_directory():
    """
    Delete everything left inside TEMP_DIR when the service starts.
    """

    TEMP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    deleted = 0

    for item in TEMP_DIR.iterdir():

        try:

            if item.is_file() or item.is_symlink():

                item.unlink()

                deleted += 1

            elif item.is_dir():

                shutil.rmtree(item)

                deleted += 1

        except Exception as e:

            logger.error(
                f"Startup cleanup failed for {item}: {e}"
            )

    logger.info(
        f"Startup cleanup complete. Deleted {deleted} old items."
    )


# =========================================================
# API Routes
# =========================================================

@app.get("/")
async def root():

    return {
        "status": "online",
        "service": "telegram-video-monitor"
    }


@app.get("/health")
async def health():

    return {
        "status": "ok",
        "processing": len(processing_messages),
        "temp_dir": str(TEMP_DIR)
    }


# =========================================================
# Delete File API
# =========================================================

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
            "status": "already_deleted"
        }

    try:

        file_path.unlink()

        logger.info(
            f"Deleted file through API: {filename}"
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


# =========================================================
# Send Video To n8n
# =========================================================

async def send_to_n8n(
    file_path: Path,
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

    # IMPORTANT:
    # Get the file size directly from the filesystem.
    #
    # This does NOT load the video into RAM.
    #
    file_size = file_path.stat().st_size

    logger.info(
        f"Preparing upload | "
        f"message={message.id} | "
        f"file={filename} | "
        f"size={file_size / 1024 / 1024:.2f} MB"
    )

    # =====================================================
    # Metadata sent to n8n
    # =====================================================

    data = {

        "message_id": str(
            message.id
        ),

        "filename": filename,

        "caption": (
            message.message or ""
        ),

        "channel_username": (
            channel_username or ""
        ),

        "channel_title": (
            channel_title or ""
        ),

        # IMPORTANT:
        # Raw size in BYTES.
        #
        # Example:
        # 156 KB -> 159744
        #
        "file_size_bytes": str(
            file_size
        ),
    }

    # =====================================================
    # Webhook Authentication
    # =====================================================

    headers = {

        "X-Webhook-Secret":
            WEBHOOK_SECRET
    }

    # =====================================================
    # HTTP Timeout
    # =====================================================

    timeout = httpx.Timeout(

        connect=30,

        read=900,

        write=900,

        pool=30
    )

    # =====================================================
    # Upload To n8n
    # =====================================================

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

            logger.info(
                f"Sending video to n8n | "
                f"message={message.id}"
            )

            response = await http.post(

                N8N_WEBHOOK_URL,

                data=data,

                files=files,

                headers=headers
            )

    logger.info(
        f"n8n response | "
        f"message={message.id} | "
        f"status={response.status_code}"
    )

    return response


# =========================================================
# Telegram New Message Handler
# =========================================================

@client.on(
    events.NewMessage(
        chats=TELEGRAM_CHANNEL,
        incoming=True
    )
)
async def new_message_handler(event):

    message = event.message

    message_id = message.id

    logger.info(
        f"Telegram message received | "
        f"id={message_id}"
    )

    # =====================================================
    # Ignore Non-Videos
    # =====================================================

    if not is_video(message):

        logger.info(
            f"Message {message_id} ignored: "
            f"not a video."
        )

        return

    # =====================================================
    # Prevent Duplicate Processing
    # =====================================================

    if message_id in processing_messages:

        logger.warning(
            f"Message {message_id} already "
            f"processing. Ignoring duplicate event."
        )

        return

    processing_messages.add(
        message_id
    )

    file_path = None

    try:

        # =================================================
        # Process Only One Video At A Time
        # =================================================

        async with process_lock:

            logger.info(
                f"Processing started | "
                f"message={message_id}"
            )

            # =============================================
            # Get Channel Information
            # =============================================

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

            # =============================================
            # Check Telegram File Size
            # =============================================

            telegram_size = None

            try:

                telegram_size = (
                    message.file.size
                )

            except Exception:

                telegram_size = None

            if telegram_size:

                logger.info(
                    f"Telegram file size | "
                    f"{telegram_size / 1024 / 1024:.2f} MB"
                )

                if telegram_size > MAX_VIDEO_BYTES:

                    logger.error(
                        f"Video rejected: "
                        f"file is larger than "
                        f"{MAX_VIDEO_MB} MB"
                    )

                    return

            # =============================================
            # Create Safe Filename
            # =============================================

            original_filename = safe_filename(
                get_filename(
                    message,
                    message_id
                )
            )

            filename = (
                f"{message_id}_"
                f"{original_filename}"
            )

            file_path = (
                TEMP_DIR / filename
            )

            # =============================================
            # Download Video From Telegram
            # =============================================

            logger.info(
                f"Downloading Telegram video | "
                f"message={message_id}"
            )

            downloaded = await client.download_media(

                message,

                file=str(file_path)
            )

            if not downloaded:

                logger.error(
                    f"Download failed | "
                    f"message={message_id}"
                )

                return

            # =============================================
            # Verify Download
            # =============================================

            if not file_path.exists():

                logger.error(
                    f"Downloaded file does not exist | "
                    f"{file_path}"
                )

                return

            # =============================================
            # Get Actual File Size
            # =============================================

            actual_size = file_path.stat().st_size

            logger.info(
                f"Download complete | "
                f"message={message_id} | "
                f"size={actual_size / 1024 / 1024:.2f} MB"
            )

            # =============================================
            # Final Size Check
            # =============================================

            if actual_size > MAX_VIDEO_BYTES:

                logger.error(
                    f"Downloaded file exceeds "
                    f"MAX_VIDEO_MB={MAX_VIDEO_MB}"
                )

                return

            # =============================================
            # Send To n8n
            # =============================================

            response = await send_to_n8n(

                file_path,

                message,

                channel_username,

                channel_title
            )

            # =============================================
            # Check n8n Response
            # =============================================

            if 200 <= response.status_code < 300:

                logger.info(
                    f"n8n accepted video | "
                    f"message={message_id}"
                )

            else:

                logger.error(

                    f"n8n rejected video | "
                    f"message={message_id} | "
                    f"status={response.status_code} | "
                    f"response={response.text[:1000]}"
                )

                return

    except Exception as e:

        logger.exception(
            f"Processing failed | "
            f"message={message_id} | "
            f"error={e}"
        )

    finally:

        # =================================================
        # Always Delete Temporary Video
        # =================================================

        try:

            if (
                file_path
                and file_path.exists()
            ):

                file_path.unlink()

                logger.info(
                    f"Temporary video deleted | "
                    f"message={message_id}"
                )

        except Exception as e:

            logger.error(
                f"Could not delete temporary file | "
                f"message={message_id} | "
                f"error={e}"
            )

        # =================================================
        # Remove From Processing Set
        # =================================================

        processing_messages.discard(
            message_id
        )

        logger.info(
            f"Processing finished | "
            f"message={message_id}"
        )


# =========================================================
# Telegram Worker
# =========================================================

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

    # =====================================================
    # Verify Channel Access
    # =====================================================

    try:

        entity = await client.get_entity(
            TELEGRAM_CHANNEL
        )

        logger.info(
            f"Channel found: "
            f"{getattr(entity, 'title', TELEGRAM_CHANNEL)}"
        )

    except Exception as e:

        logger.exception(
            f"Cannot access channel: "
            f"{TELEGRAM_CHANNEL}"
        )

        raise

    logger.info(
        f"Monitoring channel: "
        f"{TELEGRAM_CHANNEL}"
    )

    await client.run_until_disconnected()


# =========================================================
# FastAPI Server
# =========================================================

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


# =========================================================
# Main
# =========================================================

async def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "Telegram Video Monitor starting..."
    )

    logger.info(
        "========================================"
    )

    # Clean old files when service starts
    cleanup_temp_directory()

    # Run Telegram + API simultaneously
    await asyncio.gather(

        telegram_worker(),

        api_server()
    )


# =========================================================
# Entry Point
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Service stopped."
        )
