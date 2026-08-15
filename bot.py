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
# Silence verbose libraries
for noisy in ("httpx", "telethon", "apscheduler", "groq"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─── Required ENV VARs ─────────────────────────────────────────────────────────────
BOT_TOKEN               = os.getenv("BOT_TOKEN")
WEBHOOK_URL             = os.getenv("WEBHOOK_URL")
GSA_KEY_B64             = os.getenv("GSA_KEY_B64")
SHEET_ID                = os.getenv("SHEET_ID")
API_ID                  = os.getenv("API_ID")
API_HASH                = os.getenv("API_HASH")
TELETHON_SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")
SESSION_NAME            = os.getenv("SESSION_NAME", "my_bot_session")
GROQ_API_KEY            = os.getenv("GROQ_API_KEY") 
OWNER_USERNAME          = os.getenv("OWNER_USERNAME", "@owner")
ADMIN_ID                = os.getenv("ADMIN_ID")

# ─── Validation ───────────────────────────────────────────────────────────────────
missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not WEBHOOK_URL: missing.append("WEBHOOK_URL")
if not GSA_KEY_B64: missing.append("GSA_KEY_B64")
if not SHEET_ID: missing.append("SHEET_ID")
if not API_ID or not API_HASH: missing.append("API_ID/API_HASH")
if not TELETHON_SESSION_STRING and not SESSION_NAME: missing.append("TELETHON_SESSION_STRING or SESSION_NAME")
if not GROQ_API_KEY: missing.append("GROQ_API_KEY")

if missing:
    raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

try:
    API_ID = int(API_ID)
except ValueError:
    raise RuntimeError("API_ID must be an integer.")

# ─── GROQ AI Setup ──────────────────────────────────────────────────────────────
from groq import Groq

# Initialize the Groq Client
ai_client = Groq(api_key=GROQ_API_KEY)

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

# Ensure header row
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
CONTACT_ADMIN = 5

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
    admin_id = os.getenv("ADMIN_ID")
    
    # Create the special Telegram URL that opens your profile
    admin_url = f"tg://user?id={admin_id}" if admin_id else "https://t.me/"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Get Apply Links", callback_data="get_links")],
        [InlineKeyboardButton("Submit Job/Intern Link", callback_data="submit_link")],
        [InlineKeyboardButton("Contact Admin", url=admin_url)]
    ])
    await update.message.reply_text("Welcome! Choose an option below:", reply_markup=kb)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "get_links":
        await query.message.reply_text(
            "Please select your Batch Year (Graduation Year):",
            reply_markup=get_batch_keyboard(INITIAL_START_YEAR)
        )
        return BATCH
    elif query.data == "submit_link":
        await query.message.reply_text("Please enter:\n\nName of the job or internship opportunity:")
        return JOB_NAME
    elif query.data == "restart":
        admin_id = os.getenv("ADMIN_ID")
        admin_url = f"tg://user?id={admin_id}" if admin_id else "https://t.me/"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Get Apply Links", callback_data="get_links")],
            [InlineKeyboardButton("Submit Job/Intern Link", callback_data="submit_link")],
            [InlineKeyboardButton("Contact Admin", url=admin_url)]
        ])
        await query.message.reply_text("Welcome! Choose an option below:", reply_markup=kb)
        return ConversationHandler.END
    else:
        await query.message.reply_text("Invalid option.")
        return ConversationHandler.END

async def batch_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    await update.message.reply_text(
        f"Thanks! Batch {batch} noted. I'm scanning recent posts for you..."
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
            f"Thanks! Batch {batch} noted. I'm scanning recent posts for you..."
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

# ─── CORE LOGIC: GROQ AI Filter (DYNAMIC RULES) ────────────────────────────────
async def filter_messages_with_groq(messages_data, user_batch):
    """
    Uses Groq (Llama 3.1 8B Instant) to filter messages.
    High Limits: 14,400 Requests Per Day.
    """
    if not messages_data:
        return []

    msg_json = json.dumps(messages_data, ensure_ascii=False)
    
    # ─── Dynamic Logic Calculation ───
    try:
        current_year = datetime.now().year
        batch_year = int(user_batch)
        
        # 2026 cutoff logic (works for 2024, 2025, 2026 as Job Seekers)
        if batch_year <= current_year + 1:
             eligibility_rule = """
             - ACCEPT roles for 'Fresher', 'SDE 1', 'Software Engineer', 'Associate', 'Graduate Trainee'.
             - ACCEPT roles asking for '0-1 years' or '0 years' experience.
             - REJECT roles asking for '2+ years' or 'Senior' or 'Lead'.
             """
        
        # 2027+ are Internship Seekers
        else:
            eligibility_rule = """
            - REJECT 'SDE 1', 'Full Time', 'Associate' roles.
            - ACCEPT ONLY 'Intern', 'Internship', 'Summer Intern', 'Trainee'.
            - REJECT roles mentioning any 'Years of experience' requirements.
            """
            
    except ValueError:
        eligibility_rule = "ACCEPT 'Open to all' or 'Any Batch'. REJECT 'Senior' roles."

    # ─── Prompt Construction ───
    prompt = f"""
    You are a smart assistant filtering job posts.
    Current User Batch: {user_batch}
    
    My Eligibility Rules for this specific batch:
    {eligibility_rule}
    
    Universal Rules:
    1. ACCEPT if the post mentions "Open to all", "Any Batch", "All students".
    2. ACCEPT if the post explicitly mentions {user_batch}.
    3. REJECT if the post explicitly mentions a DIFFERENT batch (e.g. User is 2027, post says "2025 only").
    4. IGNORE posts that are just conversation/spam.
    
    Messages to filter:
    {msg_json}
    
    Return ONLY a JSON list of IDs like this: {{ "ids": [123, 456] }}
    """
    
    try:
        # Run in a separate thread because Groq client is sync
        def run_groq():
            return ai_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "You are a helpful assistant. Output valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.3-70b-versatile", 
                response_format={"type": "json_object"}
            )

        chat_completion = await asyncio.to_thread(run_groq)
        result_text = chat_completion.choices[0].message.content
        
        # Parse JSON
        parsed = json.loads(result_text)
        if isinstance(parsed, dict) and "ids" in parsed:
            return parsed["ids"]
        elif isinstance(parsed, list):
            return parsed
        else:
            return []
            
    except Exception as e:
        logger.error(f"Groq AI Error: {e}")
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
                current_batch = await asyncio.to_thread(batch_sheet.cell, row_idx, 4)
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
                if len(job) >= 6 and job[4] == batch and job[5] == "approved":
                    await bot.send_message(chat_id, f"🔹 {job[2]}\n{job[3]}")
                    found_approved = True
        if not found_approved:
            pass 
    except Exception as e:
        logger.error(f"Failed to fetch approved job links: {e}")

    # 4) Telethon + Groq AI Search
    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff = now_utc - timedelta(days=2)

    if not tele_client.is_connected():
        logger.info("Telethon disconnected. Attempting to reconnect...")
        await tele_client.connect()
    
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
                await asyncio.sleep(2) 
                continue

            logger.info(f"Sending {len(candidate_messages)} messages from @{entity_username} to Groq...")
            
            relevant_ids = await filter_messages_with_groq(candidate_messages, batch)
            
            for msg_id in relevant_ids:
                if msg_id in msg_objects:
                    msg = msg_objects[msg_id]
                    post_date_ist = msg.date.astimezone(ist)
                    prefix = post_date_ist.strftime(
                        f"📢 **Found in @{entity_username}**\n🗓 {date_str} at %I:%M %p\n\n"
                    )
                    await bot.send_message(chat_id, prefix + msg.text)
                    total_found += 1
            
            # Rate Limit Delay: Groq is fast, but 3 seconds is polite to Telegram
            await asyncio.sleep(3)
                    
        except Exception as e:
            logger.error(f"Error processing @{entity_username}: {e}")
            await asyncio.sleep(3)
            continue

    admin_id = os.getenv("ADMIN_ID")
    admin_url = f"tg://user?id={admin_id}" if admin_id else "https://t.me/"
    end_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Restart", callback_data="restart")],
        [InlineKeyboardButton("👤 Contact Admin", url=admin_url)]
    ])

    if total_found == 0:
        await bot.send_message(chat_id, f"No recent posts found specifically for batch {batch} (or open-to-all).", reply_markup=end_kb)
    else:
        await bot.send_message(chat_id, "✅ That's all the relevant updates I found!", reply_markup=end_kb)

# ─── Flask app ──────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is running with Groq AI."

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
            # Add to sheet as pending. Admin/Owner must review manually in Sheets now.
            await asyncio.to_thread(
                job_links_sheet.append_row,
                [user.full_name, user.username or "", job_name, job_link, batch, "pending", datetime.now(ist).strftime("%d/%m/%Y")],
                'RAW'
            )
            
            await query.message.reply_text(f"✅ Submitted successfully! Please wait for {OWNER_USERNAME} to review it in the sheets.")
        except Exception:
            await query.message.reply_text("❌ Error submitting. Try again.")
            
        return ConversationHandler.END
    return JOB_BATCH

async def contact_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.getenv("ADMIN_ID")
    if not admin_id:
        await update.message.reply_text("Admin contact is currently unavailable.")
        return ConversationHandler.END

    user_message = update.message.text
    user = update.effective_user
    
    # Show username if they have one, otherwise show their ID
    username_str = f"(@{user.username})" if user.username else f"(ID: {user.id})"
    
    forward_text = f"📩 **New Message for Admin**\nFrom: {user.full_name} {username_str}\n\n{user_message}"
    
    try:
        # Send the message to YOU
        await context.bot.send_message(chat_id=admin_id, text=forward_text, parse_mode="Markdown")
        # Tell the user it succeeded
        await update.message.reply_text("✅ Your message has been successfully sent to the admin.")
    except Exception as e:
        logger.error(f"Failed to forward message to admin: {e}")
        await update.message.reply_text("❌ Failed to send message. Please try again later.")
        
    return ConversationHandler.END

def main():
    global loop, application, tele_client
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    defaults = Defaults(tzinfo=ZoneInfo("UTC"))
    application = ApplicationBuilder().token(BOT_TOKEN).defaults(defaults).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_handler),
            CallbackQueryHandler(menu_handler, pattern="^(get_links|submit_link|restart)$"),
        ],
        states={
            BATCH: [
                CallbackQueryHandler(batch_callback_handler), 
                MessageHandler(filters.TEXT & ~filters.COMMAND, batch_text_handler)
            ],
            JOB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, job_name_handler)],
            JOB_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, job_link_handler)],
            JOB_BATCH: [CallbackQueryHandler(job_batch_handler)],
            CONTACT_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_admin_handler)],
        },
        fallbacks=[],
        allow_reentry=True
    )
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(batch_callback_handler, pattern="^(select:|page:)"))

    loop.run_until_complete(application.initialize())
    
    # Attempt to use StringSession first, fallback to SESSION_NAME
    session_data = StringSession(TELETHON_SESSION_STRING) if TELETHON_SESSION_STRING else SESSION_NAME
    tele_client = TelegramClient(session_data, API_ID, API_HASH)
    loop.run_until_complete(tele_client.connect())
    
    loop.run_until_complete(application.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}"))
    threading.Thread(target=run_flask, daemon=True).start()
    
    loop.run_forever()

if __name__ == "__main__":
    main()
