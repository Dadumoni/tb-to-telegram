import asyncio
import aiohttp
import logging
import os
import urllib.parse
import time
import re
import mimetypes
from typing import Optional, Dict, Any
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, Defaults
from telegram.constants import ParseMode
from motor.motor_asyncio import AsyncIOMotorClient

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = "8145786109:AAEl81dTqcUj0gbaCzo6pBuhfQv-dBttGYQ"

# Terabox APIs - Multiple options
TERABOX_APIS = {
    "api_one": {
        "url": "https://my-noor-queen-api.woodmirror.workers.dev",
        "name": "Noor Queen API"
    },
    "api_two": {
        "url": "https://silent-noor-stream-api.woodmirror.workers.dev/api",
        "name": "Silent Noor API"
    },
    "api_three": {
        "url": "https://terabox-pro-api.vercel.app/api",
        "name": "Terabox Pro API"
    },
    "api_four": {
        "url": "https://angel-noor-terabox-api.woodmirror.workers.dev/api",
        "name": "Angel Noor API"
    }
}

# Default API
CURRENT_API = "api_one"
TERABOX_API = TERABOX_APIS[CURRENT_API]["url"]

HYDRAX_UPLOAD_API = "http://up.hydrax.net/1c7beabe036322d38c466f7c3dca9818"
MONGODB_URI = "mongodb+srv://ftolbots_db_user:w1NU4MV2BUvYtlxx@cluster0.kihpunx.mongodb.net/?retryWrites=true&w=majority"

# Channels
TELEGRAM_CHANNEL_ID = -1002899223439    # File upload channel (downloaded file)
RESULT_CHANNEL_ID = -1002893915090      # User media copy channel

ARIA2_RPC_URL = "http://localhost:6800/jsonrpc"
ARIA2_SECRET = "mysecret"
DOWNLOAD_DIR = "/tmp/aria2_downloads"

TERABOX_DOMAINS = [
    "terabox.com", "1024terabox.com", "teraboxapp.com", "teraboxlink.com",
    "terasharelink.com", "terafileshare.com", "1024tera.com", "1024tera.cn",
    "teraboxdrive.com", "dubox.com"
]

# MongoDB Client
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["terabox_bot"]
files_collection = db["files"]
settings_collection = db["settings"]

# --- Database setup ---
async def init_db():
    """Create index for file_name field"""
    try:
        await files_collection.create_index("file_name", unique=True)
        # Initialize default API setting
        existing = await settings_collection.find_one({"key": "current_api"})
        if not existing:
            await settings_collection.insert_one({"key": "current_api", "value": CURRENT_API})
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

async def get_current_api():
    """Get current API from database"""
    try:
        setting = await settings_collection.find_one({"key": "current_api"})
        if setting:
            return setting["value"]
        return CURRENT_API
    except Exception as e:
        logger.error(f"Error getting current API: {e}")
        return CURRENT_API

async def set_current_api(api_key: str):
    """Set current API in database"""
    try:
        await settings_collection.update_one(
            {"key": "current_api"},
            {"$set": {"value": api_key}},
            upsert=True
        )
        logger.info(f"API switched to: {api_key}")
        return True
    except Exception as e:
        logger.error(f"Error setting current API: {e}")
        return False

async def is_file_processed(file_name: str) -> Optional[Dict]:
    """Check if file exists in database and return file data if found"""
    try:
        return await files_collection.find_one({"file_name": file_name})
    except Exception as e:
        logger.error(f"Database query error: {e}")
        return None

async def save_file_data(file_name: str, file_size: str, urlIframe: str):
    """Save file data to MongoDB"""
    try:
        await files_collection.insert_one({
            "file_name": file_name,
            "file_size": file_size,
            "urlIframe": urlIframe
        })
        logger.info(f"Saved to DB: {file_name}")
    except Exception as e:
        logger.error(f"Error saving to database: {e}")

# ---------------- Aria2Client ----------------
class Aria2Client:
    def __init__(self, rpc_url: str, secret: Optional[str] = None):
        self.rpc_url = rpc_url
        self.secret = secret
        self.session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _call_rpc(self, method: str, params: list = None):
        if params is None:
            params = []
        if self.secret:
            params.insert(0, f"token:{self.secret}")
        payload = {"jsonrpc": "2.0", "id": f"aria2_{int(time.time())}", "method": method, "params": params}
        try:
            await self.init_session()
            async with self.session.post(self.rpc_url, json=payload) as r:
                result = await r.json()
                if "error" in result:
                    return {"success": False, "error": result["error"]}
                return {"success": True, "result": result.get("result")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def add_download(self, url: str, options: Dict[str, Any] = None):
        if options is None:
            options = {}
        opts = {"dir": DOWNLOAD_DIR, "continue": "true"}
        opts.update(options)
        return await self._call_rpc("aria2.addUri", [[url], opts])

    async def wait_for_download(self, gid: str):
        while True:
            status = await self._call_rpc("aria2.tellStatus", [gid])
            if not status["success"]:
                return status
            info = status["result"]
            if info["status"] == "complete":
                return {"success": True, "files": info["files"]}
            elif info["status"] in ["error", "removed"]:
                return {"success": False, "error": info.get("errorMessage", "Download failed")}
            await asyncio.sleep(2)

# ---------------- Bot Logic ----------------
class TeraboxHydraxBot:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.aria2 = Aria2Client(ARIA2_RPC_URL, ARIA2_SECRET)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))

    def is_terabox_url(self, url: str) -> bool:
        try:
            domain = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
            return any(d in domain for d in TERABOX_DOMAINS)
        except:
            return False

    async def download_from_terabox(self, url: str):
        await self.init_session()
        
        # Get current API dynamically
        current_api_key = await get_current_api()
        api_url = TERABOX_APIS[current_api_key]["url"]
        
        # Different APIs have different parameter names
        if current_api_key == "api_three":
            params = {"link": url}
        else:
            params = {"url": url}
        
        try:
            async with self.session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await r.json()
                
                # Parse response based on API
                if current_api_key == "api_three":
                    # API 3 has different structure
                    if data.get("status") == "✅ Success" and data.get("📋 Extracted Info"):
                        info = data["📋 Extracted Info"][0]
                        normalized_data = {
                            "file_name": info.get("📄 Title", "unknown"),
                            "file_size": info.get("📦 Size", "0 MB"),
                            "download_link": info.get("🔗 Direct Download Link"),
                            "status": "✅ Successfully"
                        }
                        return {"success": True, "data": normalized_data}
                else:
                    # API 1, 2, 4 have similar structure
                    if data.get("status") in ["✅ Successfully", "✅ Success"]:
                        return {"success": True, "data": data}
                
                return {"success": False, "error": data.get("status", "Unknown error")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def upload_file_to_hydrax(self, file_path: str):
        await self.init_session()
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=os.path.basename(file_path))
            async with self.session.post(HYDRAX_UPLOAD_API, data=form) as r:
                data = await r.json()
                if data.get("status"):
                    return {"success": True, "data": data}
                return {"success": False, "error": data.get("msg")}

# ---------------- Handlers ----------------
bot_instance = TeraboxHydraxBot()

async def schedule_delete(msg, user_msg, delay=1200):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass
    try:
        await user_msg.delete()
    except:
        pass

async def process_link(url: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    try:
        m = update.effective_message
        if not m:
            return None, None

        tb = await bot_instance.download_from_terabox(url)
        if not tb["success"]:
            err_msg = await m.reply_text(
                f"❌ <b>{url}</b>\n{tb['error']}", parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        data = tb["data"]
        file_name = data.get("file_name", "unknown")

        # Check if file already processed
        existing_file = await is_file_processed(file_name)
        if existing_file:
            err_msg = await m.reply_text(
                f"⚠️ File <b>{file_name}</b> already processed ({existing_file.get('file_size', 'N/A')})",
                parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        file_size_str = data.get("file_size", "0")

        try:
            size_val, size_unit = file_size_str.split()
            size_val = float(size_val)
            if size_unit.lower().startswith("kb"):
                size_mb = size_val / 1024
            elif size_unit.lower().startswith("mb"):
                size_mb = size_val
            elif size_unit.lower().startswith("gb"):
                size_mb = size_val * 1024
            else:
                size_mb = 0
        except Exception:
            size_mb = 0

        if size_mb > 50:
            err_msg = await m.reply_text(
                f"❌ File <b>{file_name}</b> size {file_size_str} hai.\nLimit 50MB hai, isliye skip kiya gaya.",
                parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        dl_url = data.get("streaming_url") or data.get("download_link")
        if not dl_url:
            err_msg = await m.reply_text(
                f"❌ <b>{file_name}</b>\nNo download link", parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        dl = await bot_instance.aria2.add_download(dl_url, {"out": file_name})
        if not dl["success"]:
            err_msg = await m.reply_text(
                f"❌ <b>{file_name}</b>\n{dl['error']}", parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        gid = dl["result"]
        done = await bot_instance.aria2.wait_for_download(gid)
        if not done["success"]:
            err_msg = await m.reply_text(
                f"❌ <b>{file_name}</b>\n{done['error']}", parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        fpath = done["files"][0]["path"]

        up = await bot_instance.upload_file_to_hydrax(fpath)
        if not up["success"]:
            err_msg = await m.reply_text(
                f"❌ <b>{file_name}</b>\nUpload failed: {up['error']}", parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return None, None

        urlIframe = up["data"].get("urlIframe")
        slug = up["data"].get("slug")

        caption_file = (
            f"File Name : {file_name}\n"
            f"File Size : {file_size_str}\n"
            f"URLIframe : {urlIframe}\n"
            f"Slug : {slug}"
        )
        try:
            mime_type, _ = mimetypes.guess_type(fpath)
            with open(fpath, "rb") as f:
                if mime_type and mime_type.startswith("video"):
                    await context.bot.send_video(chat_id=TELEGRAM_CHANNEL_ID, video=f, caption=caption_file)
                elif mime_type and mime_type.startswith("image"):
                    await context.bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=f, caption=caption_file)
                else:
                    await context.bot.send_document(chat_id=TELEGRAM_CHANNEL_ID, document=f, caption=caption_file)
        except Exception as e:
            logger.warning(f"Failed to send file to TELEGRAM_CHANNEL_ID: {e}")

        # Save to MongoDB
        await save_file_data(file_name, file_size_str, urlIframe)

        # Return simple result for combining multiple links
        result_text = f"✅ {file_name} ({file_size_str})\n🔗 {urlIframe}"

        try:
            os.remove(fpath)
        except:
            pass

        return "ok", result_text

    except Exception as e:
        m = update.effective_message
        if not m:
            return None, None
        err_msg = await m.reply_text(
            f"❌ Error processing link: {str(e)}", parse_mode=ParseMode.HTML
        )
        asyncio.create_task(schedule_delete(err_msg, m))
        return None, None

async def handle_media_with_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m:
        return

    processing_msg = await m.reply_text("📤 Processing...", parse_mode=ParseMode.HTML)

    try:
        caption = m.caption or ""
        urls = re.findall(r"https?://[^\s]+", caption)
        urls = list(dict.fromkeys(urls))  # Remove duplicates

        if not urls:
            err_msg = await processing_msg.edit_text("❌ No links found in caption.", parse_mode=ParseMode.HTML)
            asyncio.create_task(schedule_delete(err_msg, m))
            return

        terabox_links = [u for u in urls if bot_instance.is_terabox_url(u)]

        if not terabox_links:
            err_msg = await processing_msg.edit_text("❌ Not supported domain. Please send a valid Terabox link.", parse_mode=ParseMode.HTML)
            asyncio.create_task(schedule_delete(err_msg, m))
            return

        # Process all links
        processed_results = []
        total_links = len(terabox_links)
        
        for idx, link in enumerate(terabox_links, 1):
            await processing_msg.edit_text(
                f"📤 Processing link {idx}/{total_links}...", 
                parse_mode=ParseMode.HTML
            )
            
            status, result_caption = await process_link(link, context, update)
            
            if status:
                processed_results.append(result_caption)
        
        if not processed_results:
            await processing_msg.edit_text("❌ No links were processed successfully.", parse_mode=ParseMode.HTML)
            asyncio.create_task(schedule_delete(processing_msg, m))
            return

        # Combine all results
        final_caption = "📦 Processed Links:\n\n" + "\n\n".join(processed_results)

        try:
            await context.bot.copy_message(
                chat_id=RESULT_CHANNEL_ID,
                from_chat_id=m.chat.id,
                message_id=m.message_id,
                caption=final_caption,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to copy media: {e}")

        try: await processing_msg.delete()
        except: pass
        try: await m.delete()
        except: pass

    except Exception as e:
        logger.error(f"Error: {e}")
        try:
            err_msg = await processing_msg.edit_text(f"❌ Error: {str(e)}", parse_mode=ParseMode.HTML)
            asyncio.create_task(schedule_delete(err_msg, m))
        except:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m:
        return
    
    current_api_key = await get_current_api()
    api_name = TERABOX_APIS[current_api_key]["name"]
    
    welcome_text = f"""
🤖 <b>Terabox to Hydrax Bot</b>

✅ <b>Current API:</b> {api_name}

📝 <b>Commands:</b>
/api_one - Switch to Noor Queen API
/api_two - Switch to Silent Noor API
/api_three - Switch to Terabox Pro API
/api_four - Switch to Angel Noor API

💡 <b>How to use:</b>
Send media (photo/video/document) with Terabox links in caption.
Bot will process all links and upload to Hydrax!

⚙️ <b>Features:</b>
• Multiple link processing
• Duplicate file detection
• Auto upload to channels
• 50MB file size limit
"""
    await m.reply_text(welcome_text, parse_mode=ParseMode.HTML)

async def switch_api(update: Update, context: ContextTypes.DEFAULT_TYPE, api_key: str):
    m = update.effective_message
    if not m:
        return
    
    if api_key not in TERABOX_APIS:
        await m.reply_text("❌ Invalid API key!", parse_mode=ParseMode.HTML)
        return
    
    success = await set_current_api(api_key)
    if success:
        api_name = TERABOX_APIS[api_key]["name"]
        await m.reply_text(
            f"✅ API switched to: <b>{api_name}</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await m.reply_text("❌ Failed to switch API!", parse_mode=ParseMode.HTML)

async def api_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await switch_api(update, context, "api_one")

async def api_two(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await switch_api(update, context, "api_two")

async def api_three(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await switch_api(update, context, "api_three")

async def api_four(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await switch_api(update, context, "api_four")

async def startup(application):
    """Initialize database on startup"""
    await init_db()
    logger.info("Bot started successfully")

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .build()
    )

    async def handle_media_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.application.create_task(handle_media_with_links(update, context))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("api_one", api_one))
    app.add_handler(CommandHandler("api_two", api_two))
    app.add_handler(CommandHandler("api_three", api_three))
    app.add_handler(CommandHandler("api_four", api_four))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle_media_wrapper
    ))

    # Initialize database before starting
    app.post_init = startup

    app.run_polling()

if __name__ == "__main__":
    main()
