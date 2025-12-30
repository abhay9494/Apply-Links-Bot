import os
import logging
import asyncio
import base64
import threading
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, request

# ─── Load environment variables ─────────────────────────────────────────────────
load_dotenv()

# ─── Timezone setup ──────────────────────────────────────────────────────────────
ist = ZoneInfo("Asia/Kolkata")

# ─── Logging ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
# Silence overly‐verbose libraries
for noisy in ("httpx", "telethon", "apscheduler", "google.ai.generativelanguage"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─── Required ENV VARs ─────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN")
WEBHOOK_URL     = os.getenv("WEBHOOK_URL")
GSA_KEY_B64     = os.getenv("GSA_KEY_B64")
SHEET_ID        = os.getenv("SHEET_ID")
API_ID          = os.getenv("API_ID")
API_HASH        = os.getenv("API_HASH")
SESSION_STRING  = os.getenv("TELETHON_SESSION_STRING")
ADMIN_ID        = int(os.getenv("ADMIN_ID", 0))
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY") 

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
GROUP_USERNAME   = os.getenv("GROUP_USERNAME")
OWNER_USERNAME   = os.getenv("OWNER_USERNAME")

# ─── Validation ───────────────────────────────────────────────────────────────────
missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not WEBHOOK_URL: missing.append("WEBHOOK_URL")
if not GSA_KEY_B64: missing.append("GSA_KEY_B64")
if not SHEET_ID: missing.append("SHEET_ID")
if not API_ID or not API_HASH: missing.append("API_ID/API_HASH")
if not SESSION_STRING: missing.append("TELETHON_SESSION_STRING")
if not GEMINI_API_KEY: missing.append("GEMINI_API_KEY")

if missing:
    raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

try:
    API_ID = int(API_ID)
except ValueError:
    raise RuntimeError("API_ID must be an integer.")

# ─── Google Gemini Setup ──────────────────────────────────────────────────────────
import google.generativeai as genai

genai.configure(api_key=GEMINI_API_KEY)

# FIX 1: Reverted to 1.5-flash because 2.0 requires billing/has 0 quota for your account.
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# ─── Write GSA credentials to a temp file ──────────────────────────────────────────
creds_bytes = base64.b64decode(GSA_KEY_B64)
creds_path = "/tmp/credentials.json"
with open(creds_path, "wb") as f:
    f.write(creds_bytes)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path

# ─── Google Sheets setup ──────────────────────────────────────────────────────────
import gspread

try:
    gc = gspread.service_account(filename=creds_path)
    sh = gc.open_by_key(SHEET_ID)
except Exception as e:
    logger.error(f"❌ Could not open Google Sheet {SHEET_ID}: {e}")
    raise

# 1. Current Updates sheet
current_updates_worksheet_title = "Current Updates"
try:
    current_updates_sheet = sh.worksheet(current_updates_worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    current_updates_sheet = sh.add_worksheet(title=current_updates_worksheet_title, rows="1000", cols="6")

# Ensure header row (Added User ID column)
try:
    current_updates_sheet.update(
        values=[["Name", "Username", "User ID", "Batch", "Date", "Time"]],
        range_name="A1:F1"
    )
except Exception as e:
    logger.warning(f"⚠️ Could not set header row in Current Updates Sheet: {e}")

# 2. Job Links sheet
job_links_worksheet_title = "Job Links"
try:
    job_links_sheet = sh.worksheet(job_links_worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    job_links_sheet = sh.add_worksheet(title=job_links_worksheet_title, rows="1000", cols="7")

try:
    job_links_sheet.update(
        values=[["Name", "Username", "Job/Intern Name", "Link", "Batch Year", "Status", "Date"]],
        range_name="A1:G1"
    )
except Exception as e:
    logger.warning(f"⚠️ Could not set header row in Job Links Sheet: {e}")

# 3. Batch sheet
batch_worksheet_title = "Batch"
try:
    batch_sheet = sh.worksheet(batch_worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    batch_sheet = sh.add_worksheet(title=batch_worksheet_title, rows="1000", cols="4")

try:
    batch_sheet.update(
        values=[["Name", "Username", "User ID", "Batch"]],
        range_name="A1:D1"
    )
except Exception as e:
    logger.warning(f"⚠️ Could not set header row in Batch Sheet: {e}")

# ─── Placeholder for Telethon client ────────────────────────────────────────────
from telethon import TelegramClient
from telethon.sessions import StringSession

tele_client: TelegramClient = None

# ─── python-telegram-bot setup ───────────────────────────────────────────────────
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    Defaults,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# Conversation states
BATCH = 1
JOB_NAME = 2
JOB_LINK = 3
JOB_BATCH = 4

INITIAL_START_YEAR = 2021

def get_batch_keyboard(start_year: int) -> InlineKeyboardMarkup:
    keyboard = []
    for row_idx in range(3):
        row_buttons = []
        for col_idx in range(3):
            year = start_year + row_idx * 3 + col_idx
            row_buttons.append(
                InlineKeyboardButton(str(year), callback_data=f"select:{year}")
            )
        keyboard.append(row_buttons)

    tenth_year = start_year + 9
    bottom_row = [
        InlineKeyboardButton("⏮️", callback_data=f"page:{start_year - 10}"),
        InlineKeyboardButton(str(tenth_year), callback_data=f"select:{tenth_year}"),
        InlineKeyboardButton("⏭️", callback_data=f"page:{start_year + 10}"),
    ]
    keyboard.append(bottom_row)
    return InlineKeyboardMarkup(keyboard)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Get Apply Links", callback_data="get_links")],
        [InlineKeyboardButton("Submit Job/Intern Link", callback_data="submit_link")]
    ])
    await update.message.reply_text("Welcome! Choose an option below:", reply_markup=kb)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "get_links":
        # Direct to batch selection (removed Force Join check)
        await query.message.reply_text(
            "Please select your Batch Year (Graduation Year):",
            reply_markup=get_batch_keyboard(INITIAL_START_YEAR)
        )
        return BATCH
    elif query.data == "submit_link":
        await query.message.reply_text("Please enter:\n\nName of the job or internship opportunity:")
        return JOB_NAME
    else:
        await query.message.reply_text("Invalid option.")
        return ConversationHandler.END

async def batch_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    await update.message.reply_text(
        f"Thanks! Batch {batch} noted. I'm scanning recent posts using AI..."
    )

    asyncio.create_task(
        fetch_and_send_apply_links(context.bot, chat_id, user.full_name, user.username or "", user.id, batch)
    )
    return ConversationHandler.END

async def batch_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    chat_id = query.message.chat_id
    
    await query.answer()

    if data.startswith("select:"):
        batch = data.split(":", 1)[1]
        await query.message.reply_text(
            f"Thanks! Batch {batch} noted. I'm scanning recent posts for you using AI..."
        )
        asyncio.create_task(
            fetch_and_send_apply_links(context.bot, chat_id, user.full_name, user.username or "", user.id, batch)
        )
        return ConversationHandler.END

    elif data.startswith("page:"):
        try:
            new_start = int(data.split(":", 1)[1])
            await query.edit_message_reply_markup(reply_markup=get_batch_keyboard(new_start))
        except ValueError:
            pass
        return BATCH
    return BATCH

# ─── CORE LOGIC: Gemini AI Filter ────────────────────────────────────────────────
async def filter_messages_with_gemini(messages_data, user_batch):
    """
    Sends a batch of messages to Gemini to determine relevance.
    messages_data: list of dicts [{'id': 123, 'text': '...'}, ...]
    Returns: List of IDs that are RELEVANT.
    """
    if not messages_data:
        return []

    # Construct a clean JSON-like string for the prompt
    msg_json = json.dumps(messages_data, ensure_ascii=False)
    
    prompt = f"""
    You are a helpful assistant filtering job posts for a student.
    
    Current User Batch: {user_batch}
    
    Here is a list of recent Telegram messages (JSON format). 
    Return a JSON LIST of the 'id's of the messages that are relevant for this user.
    
    Rules for Relevance:
    1. REJECT if the post explicitly mentions a DIFFERENT batch (e.g. if User is 2025, but post says "2026 only").
    2. ACCEPT if the post mentions "Open to all", "Any Batch", "All students", or has NO batch year mentioned at all (e.g. generic startup/Mercor posts).
    3. ACCEPT if the post explicitly mentions {user_batch}.
    4. IGNORE posts that are just conversation/spam and not job opportunities.
    
    Messages:
    {msg_json}
    
    Return ONLY valid JSON (e.g. [1234, 5678]).
    """
    
    try:
        # Generate content
        response = await ai_model.generate_content_async(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        # Parse result
        relevant_ids = json.loads(response.text)
        return relevant_ids
    except Exception as e:
        logger.error(f"Gemini AI Error: {e}")
        # If we hit a Rate Limit (429), we might want to wait, but for now just return empty
        # to avoid crashing the whole bot loop.
        return []

async def fetch_and_send_apply_links(bot, chat_id, full_name, username, user_id, batch):
    now = datetime.now(ist)
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%I:%M:%S %p")

    # 1) Append to Google Sheets
    try:
        await asyncio.to_thread(
            current_updates_sheet.append_row,
            [full_name, username, str(user_id), batch, date_str, time_str],
            'RAW'
        )
    except Exception as e:
        logger.error(f"Failed to append to Current Updates Sheet: {e}")

    # 2) Update Batch sheet
    try:
        if username:
            cell = await asyncio.to_thread(batch_sheet.find, username, in_column=2)
            if not cell:
                await asyncio.to_thread(
                    batch_sheet.append_row,
                    [full_name, username, str(user_id), batch],
                    'RAW'
                )
            else:
                row_idx = cell.row
                current_batch = await asyncio.to_thread(batch_sheet.cell, row_idx, 4) # Column D
                if current_batch.value != batch:
                    await asyncio.to_thread(batch_sheet.update_cell, row_idx, 4, batch)
    except Exception as e:
        logger.error(f"Failed to update Batch sheet: {e}")

    # 3) Approved Jobs from Sheet
    try:
        job_links_data = await asyncio.to_thread(job_links_sheet.get_all_values)
        found_approved = False
        if len(job_links_data) > 1:
            await bot.send_message(chat_id, f"📋 **Verified Links for {batch}:**", parse_mode="Markdown")
            for job in job_links_data[1:]:
                # job: [Name, Username, JobName, Link, Batch, Status, Date]
                if len(job) >= 6 and job[4] == batch and job[5] == "approved":
                    await bot.send_message(chat_id, f"🔹 {job[2]}\n{job[3]}")
                    found_approved = True
            
        if not found_approved:
            pass 
    except Exception as e:
        logger.error(f"Failed to fetch approved job links: {e}")

    # 4) Telethon + Gemini AI Search
    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff = now_utc - timedelta(days=2)
    
    try:
        with open("groups.txt", encoding="utf-8") as gf:
            group_usernames = [line.strip().split("/")[-1] for line in gf if line.strip() and not line.strip().startswith("#")]
    except Exception:
        group_usernames = []

    total_found = 0

    for entity_username in group_usernames:
        try:
            entity = await tele_client.get_entity(entity_username)
            
            candidate_messages = [] 
            msg_objects = {} 
            
            async for msg in tele_client.iter_messages(entity, limit=200):
                if not msg.text: continue
                if msg.date.astimezone(ZoneInfo("UTC")) < cutoff: continue
                if len(msg.text) < 10: continue

                candidate_messages.append({
                    "id": msg.id,
                    "text": msg.text[:1000] 
                })
                msg_objects[msg.id] = msg
            
            if not candidate_messages:
                # FIX 2: Even if no messages, we MUST sleep to prevent looping too fast to the next group
                await asyncio.sleep(5) 
                continue

            logger.info(f"Sending {len(candidate_messages)} messages from @{entity_username} to Gemini...")
            
            # Call AI
            relevant_ids = await filter_messages_with_gemini(candidate_messages, batch)
            
            # Send results
            for msg_id in relevant_ids:
                if msg_id in msg_objects:
                    msg = msg_objects[msg_id]
                    post_date_ist = msg.date.astimezone(ist)
                    prefix = post_date_ist.strftime(
                        f"📢 **Found in @{entity_username}**\n🗓 {date_str} at %I:%M %p\n\n"
                    )
                    await bot.send_message(chat_id, prefix + msg.text)
                    total_found += 1
            
            # FIX 2: Rate Limit Delay - Sleep 5 seconds after every AI Call to respect the Free Tier limits
            await asyncio.sleep(5)
                    
        except Exception as e:
            logger.error(f"Error processing @{entity_username}: {e}")
            # Even on error, sleep before next iteration to be safe
            await asyncio.sleep(5)
            continue

    if total_found == 0:
        await bot.send_message(chat_id, f"No recent posts found specifically for batch {batch} (or open-to-all).")
    else:
        await bot.send_message(chat_id, "✅ That's all the relevant updates I found!")

# ─── Flask app ──────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is running with AI."

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ─── Main ───────────────────────────────────────────────────────────────────────
async def job_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["job_name"] = update.message.text.strip()
    await update.message.reply_text("Please enter:\n\nLink to the job/internship:")
    return JOB_LINK

async def job_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["job_link"] = update.message.text.strip()
    await update.message.reply_text("Select the batch year:", reply_markup=get_batch_keyboard(INITIAL_START_YEAR))
    return JOB_BATCH

async def job_batch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("select:"):
        batch = data.split(":", 1)[1]
        job_name = context.user_data.get("job_name", "")
        job_link = context.user_data.get("job_link", "")
        user = query.from_user
        
        try:
            await asyncio.to_thread(
                job_links_sheet.append_row,
                [user.full_name, user.username or "", job_name, job_link, batch, "pending", datetime.now(ist).strftime("%d/%m/%Y")],
                'RAW'
            )
            
            row_index = len(await asyncio.to_thread(job_links_sheet.get_all_values)) - 1
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Approve", callback_data=f"approve:{row_index}"),
                 InlineKeyboardButton("Decline", callback_data=f"decline:{row_index}")]
            ])
            await context.bot.send_message(
                ADMIN_ID, 
                f"📝 **New Submission**\nFrom: {user.full_name}\nJob: {job_name}\nBatch: {batch}\nLink: {job_link}",
                reply_markup=kb, parse_mode="Markdown"
            )
            await query.message.reply_text("✅ Submitted for approval!")
        except Exception:
            await query.message.reply_text("❌ Error submitting. Try again.")
            
        return ConversationHandler.END
    return JOB_BATCH

async def admin_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return

    action, row_str = query.data.split(":")
    row_idx = int(row_str)
    
    status = "approved" if action == "approve" else "declined"
    
    try:
        await asyncio.to_thread(job_links_sheet.update_cell, row_idx + 1, 6, status)
        await query.message.edit_text(f"Submission has been **{status}**.")
    except Exception as e:
        logger.error(f"Admin action failed: {e}")
        await query.message.edit_text("Failed to update sheet.")

def main():
    global loop, application, tele_client
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    defaults = Defaults(tzinfo=ZoneInfo("UTC"))
    application = ApplicationBuilder().token(BOT_TOKEN).defaults(defaults).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_handler),
            CallbackQueryHandler(menu_handler, pattern="^(get_links|submit_link)$"),
        ],
        states={
            BATCH: [CallbackQueryHandler(batch_callback_handler), MessageHandler(filters.TEXT, batch_text_handler)],
            JOB_NAME: [MessageHandler(filters.TEXT, job_name_handler)],
            JOB_LINK: [MessageHandler(filters.TEXT, job_link_handler)],
            JOB_BATCH: [CallbackQueryHandler(job_batch_handler)],
        },
        fallbacks=[],
        allow_reentry=True
    )
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(batch_callback_handler, pattern="^(select:|page:)"))
    application.add_handler(CallbackQueryHandler(admin_action_handler, pattern="^(approve|decline):"))

    loop.run_until_complete(application.initialize())
    
    tele_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    loop.run_until_complete(tele_client.connect())
    
    loop.run_until_complete(application.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}"))
    threading.Thread(target=run_flask, daemon=True).start()
    
    loop.run_forever()

if __name__ == "__main__":
    main()
