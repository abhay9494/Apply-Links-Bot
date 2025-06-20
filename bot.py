import os
import logging
import asyncio
import base64
import threading
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
# Silence overly‐verbose libraries by default
for noisy in ("httpx", "telethon", "apscheduler"):
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

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
GROUP_USERNAME   = os.getenv("GROUP_USERNAME")
OWNER_USERNAME   = os.getenv("OWNER_USERNAME")

# ─── Validation ───────────────────────────────────────────────────────────────────
missing = []
if not BOT_TOKEN:
    missing.append("BOT_TOKEN")
if not WEBHOOK_URL:
    missing.append("WEBHOOK_URL")
if not GSA_KEY_B64:
    missing.append("GSA_KEY_B64")
if not SHEET_ID:
    missing.append("SHEET_ID")
if not API_ID or not API_HASH:
    missing.append("API_ID/API_HASH")
if not SESSION_STRING:
    missing.append("TELETHON_SESSION_STRING")

if missing:
    raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

try:
    API_ID = int(API_ID)
except ValueError:
    raise RuntimeError("API_ID must be an integer.")

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

# Google Sheets setup - Create/access the three worksheets

# 1. Current Updates sheet (previously named "Apply Links")
current_updates_worksheet_title = "Current Updates"
try:
    # First try to find the sheet with the new name
    current_updates_sheet = sh.worksheet(current_updates_worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    try:
        # If not found, try to find the old sheet name
        old_worksheet_title = "Apply Links"
        current_updates_sheet = sh.worksheet(old_worksheet_title)
        # Rename the sheet to the new name
        current_updates_sheet.update_title(current_updates_worksheet_title)
        logger.info(f"Renamed worksheet from '{old_worksheet_title}' to '{current_updates_worksheet_title}'")
    except gspread.exceptions.WorksheetNotFound:
        # If neither exists, create a new one
        current_updates_sheet = sh.add_worksheet(title=current_updates_worksheet_title, rows="1000", cols="5")
        logger.info(f"Created new worksheet '{current_updates_worksheet_title}'")

# Ensure header row exists for Current Updates sheet
try:
    current_updates_sheet.update(
        values=[["Name", "Username", "Batch", "Date", "Time"]],
        range_name="A1:E1"
    )
except Exception as e:
    logger.warning(f"⚠️ Could not set header row in Current Updates Sheet: {e}")

# 2. Job Links sheet
job_links_worksheet_title = "Job Links"
try:
    job_links_sheet = sh.worksheet(job_links_worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    job_links_sheet = sh.add_worksheet(title=job_links_worksheet_title, rows="1000", cols="7")
    logger.info(f"Created new worksheet '{job_links_worksheet_title}'")

# Ensure header row exists for Job Links sheet
try:
    job_links_sheet.update(
        values=[["Name", "Username", "Job/Intern Name", "Link", "Batch Year", "Status", "Date"]],
        range_name="A1:G1"
    )
except Exception as e:
    logger.warning(f"⚠️ Could not set header row in Job Links Sheet: {e}")

# 3. Batch sheet - for tracking unique users and their batch years
batch_worksheet_title = "Batch"
try:
    batch_sheet = sh.worksheet(batch_worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    batch_sheet = sh.add_worksheet(title=batch_worksheet_title, rows="1000", cols="3")
    logger.info(f"Created new worksheet '{batch_worksheet_title}'")

# Ensure header row exists for Batch sheet
try:
    batch_sheet.update(
        values=[["Name", "Username", "Batch"]],
        range_name="A1:C1"
    )
except Exception as e:
    logger.warning(f"⚠️ Could not set header row in Batch Sheet: {e}")

# ─── Placeholder for Telethon client (initialized in main loop) ─────────────────
from telethon import TelegramClient
from telethon.sessions import StringSession

tele_client: TelegramClient = None  # will be set in main()

# ─── python-telegram-bot setup ───────────────────────────────────────────────────
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
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

INITIAL_START_YEAR = 2021  # first page offset

def get_batch_keyboard(start_year: int) -> InlineKeyboardMarkup:
    """
    Returns an InlineKeyboardMarkup with a grid of 10 consecutive graduation years,
    laid out as three rows of three plus a bottom row for the tenth year with navigation arrows.
    Each page spans start_year through start_year + 9.

    Grid example for start_year = 2021:
    2021 | 2022 | 2023
    2024 | 2025 | 2026
    2027 | 2028 | 2029
    ⏮️    | 2030 | ⏭️

    - Year buttons have callback_data "select:<year>"
    - Prev arrow "page:<new_start>" where new_start = start_year - 10
    - Next arrow "page:<new_start>" where new_start = start_year + 10
    """
    keyboard = []
    # First three rows: 3x3 grid for years
    for row_idx in range(3):
        row_buttons = []
        for col_idx in range(3):
            year = start_year + row_idx * 3 + col_idx
            row_buttons.append(
                InlineKeyboardButton(str(year), callback_data=f"select:{year}")
            )
        keyboard.append(row_buttons)

    # Tenth year (bottom-middle)
    tenth_year = start_year + 9

    # Prev arrow: always allow going back by 10 years
    prev_callback = f"page:{start_year - 10}"
    # Next arrow: always allow going forward by 10 years
    next_callback = f"page:{start_year + 10}"

    bottom_row = [
        InlineKeyboardButton("⏮️", callback_data=prev_callback),
        InlineKeyboardButton(str(tenth_year), callback_data=f"select:{tenth_year}"),
        InlineKeyboardButton("⏭️", callback_data=next_callback),
    ]
    keyboard.append(bottom_row)
    return InlineKeyboardMarkup(keyboard)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the main menu with options to get apply links or submit a new job/intern link"""
    user_id = update.effective_user.id
    
    # Main menu keyboard
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Get Apply Links", callback_data="get_links")],
        [InlineKeyboardButton("Submit Job/Intern Link", callback_data="submit_link")]
    ])
    
    await update.message.reply_text(
        "Welcome! Choose an option below:",
        reply_markup=kb
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the main menu options"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "get_links":
        await query.message.reply_text(
            "Please select your Batch Year (Graduation Year) from the options below:",
            reply_markup=get_batch_keyboard(INITIAL_START_YEAR)
        )
        return BATCH
    elif query.data == "submit_link":
        await query.message.reply_text(
            "Please enter:\n \nName of the job or internship opportunity:"
        )
        return JOB_NAME
    else:
        await query.message.reply_text("Invalid option. Please try again.")
        return ConversationHandler.END

async def check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verifies channel+group membership, then sends the batch-selection keyboard (first page)."""
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    try:
        chan_member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        grp_member = await context.bot.get_chat_member(f"@{GROUP_USERNAME}", user_id)
    except Exception as e:
        logger.error(f"Error checking membership for user {user_id}: {e}")
        await query.message.reply_text("⚠️ There was an error verifying your membership. Try again in a minute.")
        return ConversationHandler.END

    allowed_status = {ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR}
    if chan_member.status in allowed_status and grp_member.status in allowed_status:
        await query.message.reply_text(
            "✅ Thanks! \n\nPlease select your Batch Year (Graduation Year) from the options below:",
            reply_markup=get_batch_keyboard(INITIAL_START_YEAR)
        )
        return BATCH
    else:
        await query.message.reply_text("❌ You must join both the channel and group to proceed.")
        return ConversationHandler.END

async def batch_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fallback if the user types free text instead of using the keyboard.
    Keeps the existing behavior: treat entire message as 'batch' string.
    """
    batch = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id
    full_name = user.full_name
    username = user.username or ""

    logger.info(f"User @{username} ({full_name}) (text) requested batch '{batch}'")
    await update.message.reply_text(
        "Thanks! Your batch is noted. I’m fetching the Apply Links now — you’ll get them shortly."
    )

    # Schedule the fetch task on our shared loop
    asyncio.create_task(
        fetch_and_send_apply_links(
            context.bot, chat_id, full_name, username, batch
        )
    )
    return ConversationHandler.END

async def batch_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all inline-keyboard callbacks when user is in BATCH state:
    - "select:<year>" → treat <year> as batch, proceed to fetch.
    - "page:<new_start>" → edit message to show new keyboard page.
    """
    query = update.callback_query
    data = query.data  # e.g. "select:2024" or "page:2011"
    user = query.from_user
    chat_id = query.message.chat_id
    full_name = user.full_name
    username = user.username or ""

    # Always answer the callback to remove loading spinner
    await query.answer()

    if data.startswith("select:"):
        # User selected a specific year
        batch = data.split(":", 1)[1]
        logger.info(f"User @{username} ({full_name}) selected batch '{batch}' via button")
        await query.message.reply_text(
            f"Thanks! Your batch {batch} is noted. I’m fetching the Apply Links now — you’ll get them shortly."
        )
        asyncio.create_task(
            fetch_and_send_apply_links(
                context.bot, chat_id, full_name, username, batch
            )
        )
        return ConversationHandler.END

    elif data.startswith("page:"):
        # Pagination requested
        try:
            new_start = int(data.split(":", 1)[1])
        except ValueError:
            await query.message.reply_text("⚠️ Invalid page navigation.")
            return BATCH

        # Update the inline keyboard to the new page
        await query.edit_message_reply_markup(
            reply_markup=get_batch_keyboard(new_start)
        )
        return BATCH

    else:
        # Fallback for any unexpected callback_data
        return BATCH

async def fetch_and_send_apply_links(bot, chat_id, full_name, username, batch):
    """
    1) Append to local data.txt
    2) Append to Google Sheets (Current Updates)
    3) Update Batch sheet with unique user if not already present
    4) Use Telethon to search recent messages in each group from groups.txt
    5) Send a final summary message at the end
    """
    now = datetime.now(ist)
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%I:%M:%S %p")

    # 1) Local log (data.txt)
    try:
        async def write_local():
            with open("data.txt", "a", encoding="utf-8") as f:
                f.write(f"{full_name},{username},{batch},{date_str},{time_str}\n")
        await asyncio.to_thread(write_local)
        logger.debug("Appended to data.txt")
    except Exception as e:
        logger.error(f"Failed to write data.txt: {e}")

    # 2) Google Sheets - Current Updates sheet
    try:
        await asyncio.to_thread(
            current_updates_sheet.append_row,
            [full_name, username, batch, date_str, time_str],
            'RAW'
        )
        logger.debug("Appended row to Current Updates Sheet")
    except Exception as e:
        logger.error(f"Failed to append to Current Updates Sheet: {e}")

    # 3) Update Batch sheet with unique user if not already present
    try:
        # Check if user already exists in Batch sheet
        if username:  # Only proceed if username is not empty
            # Try to find the user by username
            cell = await asyncio.to_thread(
                batch_sheet.find,
                username,
                in_column=2  # Username is in column B (index 2)
            )
            
            if not cell:  # User not found, add them
                await asyncio.to_thread(
                    batch_sheet.append_row,
                    [full_name, username, batch],
                    'RAW'
                )
                logger.debug(f"Added new user @{username} to Batch sheet")
            else:
                # User exists, check if batch needs updating
                row_idx = cell.row
                current_batch = await asyncio.to_thread(
                    batch_sheet.cell,
                    row_idx, 3  # Batch is in column C (index 3)
                )
                
                if current_batch.value != batch:
                    # Update the batch
                    await asyncio.to_thread(
                        batch_sheet.update_cell,
                        row_idx, 3, batch
                    )
                    logger.debug(f"Updated batch for @{username} from {current_batch.value} to {batch}")
    except Exception as e:
        logger.error(f"Failed to update Batch sheet: {e}")

    # 4) Telethon fetch
    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff = now_utc - timedelta(days=2)
    found_any = False

    # Read groups.txt
    try:
        with open("groups.txt", encoding="utf-8") as gf:
            group_usernames = [
                line.strip().split("/")[-1]
                for line in gf
                if line.strip() and not line.strip().startswith("#")
            ]
        logger.info(f"Loaded {len(group_usernames)} groups from groups.txt")
    except FileNotFoundError:
        logger.warning("⚠️ groups.txt not found; no groups to search.")
        group_usernames = []
    except Exception as e:
        logger.error(f"Error reading groups.txt: {e}")
        group_usernames = []

    for entity_username in group_usernames:
        try:
            # Use the shared tele_client (already connected on our main loop)
            entity = await tele_client.get_entity(entity_username)
        except Exception as e:
            logger.warning(f"Could not load Telethon entity @{entity_username}: {e}")
            continue

        logger.debug(f"Searching messages in @{entity_username} since {cutoff.isoformat()}")
        try:
            async for msg in tele_client.iter_messages(entity, limit=200):
                if not msg.text:
                    continue
                post_date_utc = msg.date.astimezone(ZoneInfo("UTC"))
                if post_date_utc < cutoff:
                    continue
                if batch.lower() not in msg.text.lower():
                    continue

                post_date_ist = post_date_utc.astimezone(ist)
                prefix = post_date_ist.strftime(
                    f"This message was posted on @{entity_username} at %d/%m/%Y at %I:%M:%S %p IST.\n\n"
                )
                await bot.send_message(chat_id, prefix + msg.text)
                found_any = True
        except Exception as e:
            logger.error(f"Error iterating messages in @{entity_username}: {e}")
            continue

    # 4) Send final summary or fallback message
    if not found_any:
        await bot.send_message(
            chat_id,
            f"No recent posts (within 2 days) found for batch {batch}. "
            f"If you have questions, please DM the owner: @{OWNER_USERNAME}"
        )
        logger.info(f"No matching posts found for batch '{batch}'")
    else:
        await bot.send_message(
            chat_id,
            f"That's all I could fetch from the last 48 hours for the batch {batch}.\n"
            f"If you face any issues, DM - @{OWNER_USERNAME}\n\n"
            f"To restart click /start"
        )
        logger.info(f"Sent summary message after fetching posts for batch '{batch}'")

# ─── Flask app for webhook receiving ──────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "Hello! The Telegram bot is running."

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    """
    This route is where Telegram will POST updates. We convert them
    to a python-telegram-bot Update object, then hand off to application.process_update().
    """
    update_data = request.get_json(force=True)
    update = Update.de_json(update_data, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask on 0.0.0.0:{port}")
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ─── Bot setup (webhook mode) ─────────────────────────────────────────────────────
def main():
    global loop, application, tele_client

    # 1) Create a new asyncio event loop and set it
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 2) Build the telegram‐bot Application
    defaults = Defaults(tzinfo=ZoneInfo("UTC"))
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(10)
        .defaults(defaults)
        .concurrent_updates(500)
        .build()
    )

    # 3) Register handlers
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_handler),
            CallbackQueryHandler(menu_handler, pattern="^(get_links|submit_link)$"),
        ],
        states={
            BATCH: [
                # Handle all inline-keyboard callbacks (select:, page:)
                CallbackQueryHandler(batch_callback_handler),
                # Fallback if user still types free text
                MessageHandler(filters.TEXT & ~filters.COMMAND, batch_text_handler),
            ],
            JOB_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, job_name_handler),
            ],
            JOB_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, job_link_handler),
            ],
            JOB_BATCH: [
                CallbackQueryHandler(job_batch_handler),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    application.add_handler(conv_handler)

    # Add callback handler for batch selection outside of conversation
    application.add_handler(CallbackQueryHandler(batch_callback_handler, pattern="^(select:|page:)"))

    # Add callback handler for admin actions
    application.add_handler(CallbackQueryHandler(admin_action_handler, pattern="^(approve|decline):[0-9]+$"))
    # 4) Initialize the Application (prepares webhook, jobs, etc.)
    loop.run_until_complete(application.initialize())
    logger.info("Telegram‐bot Application initialized.")

    # 5) Initialize Telethon client on the same loop, but use connect() instead of start()
    tele_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    try:
        loop.run_until_complete(tele_client.connect())
        if not tele_client.is_connected():
            raise RuntimeError("Telethon failed to connect.")
        logger.info("✅ Telethon client connected on the same asyncio loop.")
    except Exception as e:
        logger.error(f"❌ Failed to connect Telethon client: {e}")
        raise

    # 6) Register the webhook with Telegram
    webhook_endpoint = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    try:
        loop.run_until_complete(application.bot.set_webhook(webhook_endpoint))
        logger.info(f"Webhook set to: {webhook_endpoint}")
    except Exception as e:
        logger.error(f"❌ Could not set webhook: {e}")
        raise

    # 7) Start Flask server in a background thread
    threading.Thread(target=run_flask, daemon=True).start()

    # 8) Now run the loop forever to process incoming updates
    logger.info("Bot is now listening via webhook (no more polling).")
    loop.run_forever()

async def job_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the job/intern name input"""
    job_name = update.message.text.strip()
    context.user_data["job_name"] = job_name
    
    await update.message.reply_text(
        "Please enter:\n \nLink to the job or internship opportunity:"
    )
    return JOB_LINK

async def job_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the job/intern link input"""
    job_link = update.message.text.strip()
    context.user_data["job_link"] = job_link
    
    await update.message.reply_text(
        "Please select the batch year this opportunity is for:",
        reply_markup=get_batch_keyboard(INITIAL_START_YEAR)
    )
    return JOB_BATCH

async def job_batch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the batch selection for job/intern submission"""
    query = update.callback_query
    data = query.data
    user = query.from_user
    full_name = user.full_name
    username = user.username or ""
    
    await query.answer()
    
    if data.startswith("select:"):
        batch = data.split(":", 1)[1]
        context.user_data["job_batch"] = batch
        
        # Save the job/intern link to Google Sheets with 'pending' status
        job_name = context.user_data.get("job_name", "")
        job_link = context.user_data.get("job_link", "")
        now = datetime.now(ist)
        date_str = now.strftime("%d/%m/%Y")
        
        try:
            await asyncio.to_thread(
                job_links_sheet.append_row,
                [full_name, username, job_name, job_link, batch, "pending", date_str],
                'RAW'
            )
            logger.debug("Appended job link to Google Sheet")
            
            # Notify admin about the new submission
            admin_message = (
                f"New job/intern link submission:\n\n"
                f"From: {full_name} (@{username})\n"
                f"Job/Intern: {job_name}\n"
                f"Link: {job_link}\n"
                f"Batch: {batch}\n\n"
                f"Do you want to approve this submission?"
            )
            
            # Create a unique identifier for this submission
            row_index = len(await asyncio.to_thread(job_links_sheet.get_all_values)) - 1
            
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:{row_index}"),
                    InlineKeyboardButton("Decline", callback_data=f"decline:{row_index}")
                ]
            ])
            
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_message,
                reply_markup=kb
            )
            
            await query.message.reply_text(
                f"Thank you! Your job/intern link for {job_name} has been submitted for approval. "
                f"You will be notified once it's approved."
            )
        except Exception as e:
            logger.error(f"Failed to submit job link: {e}")
            await query.message.reply_text(
                "There was an error submitting your job/intern link. Please try again later."
            )
        
        return ConversationHandler.END
    elif data.startswith("page:"):
        try:
            new_start = int(data.split(":", 1)[1])
        except ValueError:
            await query.message.reply_text("⚠️ Invalid page navigation.")
            return JOB_BATCH

        # Update the inline keyboard to the new page
        await query.edit_message_reply_markup(
            reply_markup=get_batch_keyboard(new_start)
        )
        return JOB_BATCH
    else:
        return JOB_BATCH

async def admin_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin approval/decline actions"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    await query.answer()
    
    # Only allow the admin to perform these actions
    if user_id != ADMIN_ID:
        await query.message.reply_text("You are not authorized to perform this action.")
        return
    
    action, row_index = data.split(":")
    row_index = int(row_index)
    
    try:
        # Get the submission details
        all_values = await asyncio.to_thread(job_links_sheet.get_all_values)
        if row_index >= len(all_values):
            await query.message.reply_text("Invalid submission index.")
            return
        
        submission = all_values[row_index]
        submitter_name = submission[0]
        submitter_username = submission[1]
        job_name = submission[2]
        job_link = submission[3]
        batch = submission[4]
        
        # Update the status in the sheet
        new_status = "approved" if action == "approve" else "declined"
        await asyncio.to_thread(
            job_links_sheet.update_cell,
            row_index + 1,  # +1 because sheets are 1-indexed
            6,  # Status column (F)
            new_status
        )
        
        # Notify the admin of the action taken
        await query.message.reply_text(
            f"The submission for {job_name} has been {new_status}."
        )
        
        # If approved, send the job/intern link to all users who requested that batch
        if action == "approve":
            # Get all users who requested this batch
            all_data = await asyncio.to_thread(current_updates_sheet.get_all_values)
            matching_users = []
            
            for row in all_data[1:]:  # Skip header row
                if len(row) >= 3 and row[2] == batch:  # Check batch column
                    user_name = row[0]
                    user_username = row[1]
                    matching_users.append((user_name, user_username))
            
            # TODO: Implement notification to users who requested this batch
            # This would require storing chat_ids, which is not in the current data model
            
            # For now, just log the action
            logger.info(f"Job link for {job_name} (batch {batch}) approved. Would notify {len(matching_users)} users.")
    except Exception as e:
        logger.error(f"Error processing admin action: {e}")
        await query.message.reply_text("There was an error processing your action. Please try again.")
        return ConversationHandler.END

if __name__ == "__main__":
    main()
