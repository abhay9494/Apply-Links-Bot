# Apply Links Telegram Bot

A Telegram bot that sends students the job and internship links that fit their graduation batch.
It reads the last 48 hours of posts from 20+ public job channels, lets an LLM keep only the ones
that match the student's batch, and adds links that the admin has approved.

## How it works

1. **Pick your batch.** `/start` → **Get Apply Links** → choose your graduation year on an inline
   keyboard (pages of 10 years), or type it.
2. **Verified links first.** Links from the **Job Links** sheet whose batch matches and whose status
   is `approved` are sent straight away.
3. **Fresh posts from job channels.** With a Telegram user session (Telethon), the bot reads up to
   200 recent posts from each channel in [`groups.txt`](groups.txt) and keeps those from the last
   48 hours.
4. **An LLM filters them** through the Groq API (`openai/gpt-oss-120b`, JSON mode), with rules that
   depend on the batch:
   - graduating batches (this year or next): fresher / SDE 1 / 0–1 year roles, no senior roles;
   - later batches: internships only, nothing that asks for years of experience;
   - a post that names a different batch year is rejected; "any batch" / "open to all" is kept.

   Only the message ids are returned, so the bot forwards the original posts unchanged.
5. **Everything is logged in Google Sheets** (gspread, service account):
   - **Current Updates**: every request (name, username, user id, batch, date, time);
   - **Batch**: each user's latest batch.
6. **Students can submit links.** **Submit Job/Intern Link** asks for the role, the link and the batch.
   The link is saved in **Job Links** as `pending` until the admin sets it to `approved`.
7. **Contact Admin** opens a chat with the admin.

The bot runs as a **Flask webhook** (`/<BOT_TOKEN>` receives Telegram updates), so it can be
hosted on any free web service that gives it a public URL.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in the values below
python bot.py
```

| Variable | Where to get it |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `WEBHOOK_URL` | the public URL of your deployment, e.g. `https://my-bot.onrender.com` |
| `API_ID`, `API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `TELETHON_SESSION_STRING` | a Telethon `StringSession` of the account that reads the channels (or `SESSION_NAME` for a local session file) |
| `GSA_KEY_B64` | a Google service-account JSON key, base64-encoded: save it as `credentials.json` and run `python encode_key.py` |
| `SHEET_ID` | the id in your Google Sheet's URL; share the sheet with the service account's email |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
| `ADMIN_ID` | your numeric Telegram user id (for the Contact Admin button) |
| `OWNER_USERNAME` | optional, shown after a link is submitted |
| `PORT` | optional, default `5000` |

The sheets (**Current Updates**, **Job Links**, **Batch**) and their header rows are created on the first start.
`.env` and `credentials.json` are git-ignored — never commit them.

## Files

```
bot.py            the bot: Telegram handlers, channel scan, Groq filter, Google Sheets, Flask webhook
groups.txt        the job channels to scan (one t.me link per line)
encode_key.py     turns credentials.json into the GSA_KEY_B64 value
requirements.txt
```
