# multi_user_bot.py
import asyncio
import logging
import re
import os
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

load_dotenv()
BOT_TOKEN = "8477194068:AAGsO_GcMZWumoYwr_DqjvagSxynPcndYyc"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# in-memory state while user completes OTP flow
pending_signins = {}  # user_id -> PendingSignin

# active Telethon clients per telegram user id
active_clients = {}  # user_id -> TelethonClientWrapper

# regexes
trigger_re = re.compile(r"\bbatch\s*completed\b", re.IGNORECASE)
link_re = re.compile(r"(https?://t\.me/(?:c/\d+|[A-Za-z0-9_]+)/)(\d+)-(\d+)")

@dataclass
class PendingSignin:
    api_id: int
    api_hash: str
    phone: str
    client: TelegramClient

@dataclass
class ClientWrapper:
    user_telegram_id: int
    client: TelegramClient
    task: asyncio.Task

# -------- Control bot handlers --------

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer(
        "Welcome. To register your account for automation, send /register\n"
        "You will need your API ID, API Hash (from my.telegram.org) and your phone."
    )

@dp.message_handler(commands=["register"])
async def cmd_register(message: types.Message):
    await message.answer(
        "Send your credentials in one message in this format (replace <...>):\n\n"
        "`api_id <api_id>\napi_hash <api_hash>\nphone <+country_phone>`\n\n"
        "Example:\n`api_id 123456\napi_hash abcdef0123456789\nphone +1234567890`",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda m: m.text and m.text.strip().lower().startswith("api_id"))
async def receive_credentials(message: types.Message):
    # parse credentials
    try:
        lines = [l.strip() for l in message.text.strip().splitlines() if l.strip()]
        d = {}
        for ln in lines:
            k, v = ln.split(None, 1)
            d[k.lower()] = v.strip()
        api_id = int(d["api_id"])
        api_hash = d["api_hash"]
        phone = d["phone"]
    except Exception:
        await message.reply("Couldn't parse credentials. Use the format shown by /register.")
        return

    # create a Telethon client with session filename per bot-user id
    session_name = str(SESSIONS_DIR / f"session_{message.from_user.id}")
    client = TelegramClient(session_name, api_id, api_hash)

    try:
        await client.connect()
        # request code
        sent = await client.send_code_request(phone)
    except Exception as e:
        await message.reply(f"Failed to send code request: {e}")
        await client.disconnect()
        return

    pending_signins[message.from_user.id] = PendingSignin(api_id=api_id, api_hash=api_hash, phone=phone, client=client)
    await message.reply("Code sent to your Telegram/SMS. Reply to me with `/code <12345>` (without brackets).")

@dp.message_handler(lambda m: m.text and m.text.strip().lower().startswith("/code"))
async def receive_code(message: types.Message):
    parts = message.text.strip().split(None, 1)
    if len(parts) != 2:
        await message.reply("Use: /code <the-code-you-received>")
        return
    code = parts[1].strip()

    pending = pending_signins.get(message.from_user.id)
    if not pending:
        await message.reply("No pending sign-in. Start with /register.")
        return

    client = pending.client
    phone = pending.phone
    try:
        # try sign in
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        # 2FA: request password
        await message.reply("This account has 2FA enabled. Send `/pwd <password>`.")
        # store and wait for password (we reuse pending_signins)
        return
    except PhoneCodeInvalidError:
        await message.reply("Invalid code. Try again.")
        return
    except Exception as e:
        await message.reply(f"Sign-in failed: {e}")
        await client.disconnect()
        pending_signins.pop(message.from_user.id, None)
        return

    # sign-in successful: start automation
    await message.reply("Signed in and starting automation for you.")
    pending_signins.pop(message.from_user.id, None)
    # start the per-user telethon client loop
    await start_user_client(message.from_user.id, client)

@dp.message_handler(lambda m: m.text and m.text.strip().lower().startswith("/pwd"))
async def receive_password(message: types.Message):
    # handle 2FA password for pending sign-in
    parts = message.text.strip().split(None, 1)
    if len(parts) != 2:
        await message.reply("Use: /pwd <your-2fa-password>")
        return
    password = parts[1].strip()

    pending = pending_signins.get(message.from_user.id)
    if not pending:
        await message.reply("No pending sign-in. Start with /register.")
        return

    client = pending.client
    phone = pending.phone
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await message.reply(f"2FA sign-in failed: {e}")
        await client.disconnect()
        pending_signins.pop(message.from_user.id, None)
        return

    await message.reply("Signed in and starting automation for you.")
    pending_signins.pop(message.from_user.id, None)
    await start_user_client(message.from_user.id, client)

@dp.message_handler(commands=["stop"])
async def cmd_stop(message: types.Message):
    # stop the user's client if running
    uid = message.from_user.id
    wrapper = active_clients.get(uid)
    if wrapper:
        try:
            await wrapper.client.disconnect()
            wrapper.task.cancel()
        except Exception:
            pass
        active_clients.pop(uid, None)
        await message.reply("Stopped your automation and disconnected the session.")
    else:
        await message.reply("No active automation for your account.")

# -------- Telethon automation per user --------

async def start_user_client(user_telegram_id: int, client: TelegramClient):
    """
    Register the event handler on the client's loop and keep it running.
    """
    # define handler closure
    @client.on(events.NewMessage)
    async def handler(event):
        try:
            # only care if it's a reply to a message (the message that the user sent)
            if not event.is_reply:
                return
            replied = await event.get_reply_message()
            me = await client.get_me()
            if not replied or replied.sender_id != me.id:
                return
            # trigger match - ignore emoji/formatting: check words 'batch completed'
            reply_text = (event.raw_text or event.text or "")
            if not reply_text:
                return
            if not trigger_re.search(reply_text):
                return

            # extract links from the original message (the one YOU sent)
            original_text = (replied.raw_text or replied.text or "")
            matches = link_re.findall(original_text)
            if not matches:
                return  # nothing to do

            updated_links = []
            for base_url, start_str, end_str in matches:
                start_new = int(start_str) + 30
                end_new = int(end_str) + 30
                updated_links.append(f"{base_url}{start_new}-{end_new}")

            # reply with updated links (one per line) to the Batch message
            await client.send_message(event.chat_id, "\n".join(updated_links), reply_to=event.message.id)
            logging.info("User %s: sent updated links", user_telegram_id)
        except Exception as e:
            logging.exception("Error in user client handler: %s", e)

    # Save session filename (Telethon writes file automatically)
    session_file = str(SESSIONS_DIR / f"session_{user_telegram_id}.session")
    logging.info("Session will be stored at %s", session_file)

    # Ensure the client is connected (it should be already after sign_in)
    try:
        if not await client.is_connected():
            await client.connect()
    except Exception as e:
        logging.exception("Failed to ensure client connected: %s", e)
        return

    # Start a background task to keep the client alive (run_until_disconnected)
    loop = asyncio.get_event_loop()
    task = loop.create_task(client.run_until_disconnected())
    active_clients[user_telegram_id] = ClientWrapper(user_telegram_id=user_telegram_id, client=client, task=task)
    logging.info("Started Telethon client for user %s", user_telegram_id)

# -------- startup/shutdown --------

async def on_startup(dp):
    logging.info("Bot started. Ready to accept registrations.")

async def on_shutdown(dp):
    # disconnect all clients cleanly
    for uid, wrapper in list(active_clients.items()):
        try:
            await wrapper.client.disconnect()
            wrapper.task.cancel()
        except Exception:
            pass

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
