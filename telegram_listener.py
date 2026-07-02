import requests
import time
import re
from datetime import datetime

print("STARTED", flush=True)

BOT_TOKEN      = "7890385799:AAHhQfEluupOYvgtrCQOOTBwlkko-2Jwguc"
CHAT_ID        = "-5501297384"
OPENWEBUI_URL  = "http://localhost:3000/api/chat/completions"
OPENWEBUI_KEY  = "sk-91bd167cca0142c983379ebe27b4e621"
MODEL_ID       = "chev-chelios"

# Matches only direct address — "chev" as a whole word at the start of the message,
# or after common call words (hey/yo/oi etc), or as @chev.
# Does NOT match mid-word: "chevron", "chevy", "achievement" etc.
_TRIGGER_RE = re.compile(
    r'(?:'
    r'^\s*(?:hey|yo|oi|hi|ok|okay|sup|listen|ey|bruv|mate|bro)[\s,!?]*chev\b'
    r'|^\s*chev\b'
    r'|@chev\b'
    r')',
    re.IGNORECASE,
)

def ask_chev(user_text, sender_name):
    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are in a Telegram group chat. Keep replies to 1-2 sentences max — "
                    "this is a chat, not a blog post. Plain text only, no bullet points, no headers. "
                    "Sound like a trader texting from his phone. Never sound like a chatbot."
                )
            },
            {
                "role": "user",
                "content": (
                    f"{sender_name} says in the Telegram group: \"{user_text}\"\n\n"
                    f"Reply directly to them."
                )
            }
        ]
    }
    resp = requests.post(
        OPENWEBUI_URL,
        headers={"Authorization": f"Bearer {OPENWEBUI_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)

def get_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", [])

# Clear any active webhook — Telegram rejects getUpdates with 409 if a webhook is set
try:
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=false", timeout=10)
    result = r.json()
    if result.get("result"):
        print(f"[{datetime.now()}] Webhook cleared OK. Waiting 6s for previous connections to expire...")
        time.sleep(6)
    else:
        print(f"[{datetime.now()}] deleteWebhook response: {result}")
except Exception as e:
    print(f"[{datetime.now()}] deleteWebhook failed: {e}")

print(f"[{datetime.now()}] Telegram listener online — waiting to be called directly.")
last_update_id = 0

while True:
    try:
        updates = get_updates(last_update_id + 1)
        for u in updates:
            last_update_id = u["update_id"]
            msg = u.get("message") or u.get("edited_message")
            if not msg or "text" not in msg:
                continue
            text = msg["text"]
            if not _TRIGGER_RE.search(text):
                continue
            sender = msg.get("from", {})
            sender_name = sender.get("first_name") or sender.get("username") or "Someone"
            print(f"[{datetime.now()}] Triggered by {sender_name}: {text!r}")
            try:
                reply = ask_chev(text, sender_name)
                send_telegram(reply)
                print(f"[{datetime.now()}] Replied: {reply[:120]}...")
            except Exception as e:
                print(f"[{datetime.now()}] Chev reply failed: {e}")
                send_telegram("Sorry, I'm having trouble thinking right now. Try again in a moment.")
        time.sleep(2)
    except Exception as e:
        msg = str(e)
        if "409" in msg:
            print(f"[{datetime.now()}] 409 — another connection still active. Waiting 25s...")
            time.sleep(25)
        else:
            print(f"[{datetime.now()}] Loop error: {e}")
            time.sleep(5)
