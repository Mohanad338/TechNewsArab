import os
import json
import time
import asyncio
import logging
import subprocess

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

# ---------- إعدادات ثابتة (عدّلها إذا تغيرت القنوات) ----------
SOURCE_CHANNEL = "tech"           # https://t.me/tech
TARGET_CHANNEL = "@TechNewsArab"  # قناة الهدف (النشر عبر Bot API)
CHANNEL_HANDLE_FOR_CAPTION = "@TechNewsArab"

STATE_FILE = "last_id.json"
MAX_RUNTIME_SECONDS = 5 * 3600 + 40 * 60   # 5 ساعات و40 دقيقة
MAX_BOT_UPLOAD_BYTES = 49 * 1024 * 1024    # هامش أمان تحت حد الـ50 ميجا لـ Bot API

# ---------- أسرار من البيئة (GitHub Secrets) ----------
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-flash-lite-latest:generateContent"
)

client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)


# ================= حالة last_id (محفوظة بالمستودع) =================
def load_last_id():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("last_id", 0)
        except Exception:
            return 0
    return 0


def save_last_id(new_id):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": new_id}, f)
    _git_commit_state(new_id)


def _git_commit_state(new_id):
    try:
        subprocess.run(["git", "config", "user.email", "relay-bot@users.noreply.github.com"], check=False)
        subprocess.run(["git", "config", "user.name", "relay-bot"], check=False)
        subprocess.run(["git", "add", STATE_FILE], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return  # ما فيه تغيير فعلي
        subprocess.run(["git", "commit", "-m", f"update last_id -> {new_id}"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        push = subprocess.run(["git", "push"], check=False)
        if push.returncode != 0:
            log.warning("git push فشل — تحقق من صلاحيات Workflow permissions (Read and write).")
    except Exception as e:
        log.warning(f"git commit/push فشل: {e}")


# ================= Gemini: تصنيف إعلان + ترجمة/إعادة صياغة =================
def gemini_process(text: str) -> dict:
    if not text or not text.strip():
        return {"is_ad": False, "arabic_text": ""}

    prompt = f"""أنت محرر محتوى تقني عربي محترف. مهمتك بخصوص النص التالي (منقول من قناة تلكرام إنجليزية):

1) صنّف: هل هذا النص إعلان ترويجي/دعاية/عرض تجاري/رعاية (sponsored)؟ أم خبر أو محتوى تقني عادي؟
2) إذا لم يكن إعلاناً: أعد صياغته بالكامل بالعربية الفصحى، بأسلوب طبيعي سلس كأن كاتباً عربياً كتبه من الصفر (مو ترجمة حرفية كلمة بكلمة)، مع الحفاظ الكامل على المعنى والمعلومات والأرقام التقنية.
3) احذف نهائياً أي رابط أو ذكر لاسم/يوزر قناة المصدر (مثل t.me/tech أو أي إشارة لها)، بدون أن تعلّق على أنك حذفت شيئاً. روابط أخرى غير متعلقة بقناة المصدر (مثل رابط مقال خارجي) اتركها كما هي إذا كانت مفيدة للمحتوى.

النص:
\"\"\"{text}\"\"\"

أجب حصراً بصيغة JSON صافية بدون أي نص إضافي وبدون Markdown fences، بهذا الشكل بالضبط:
{{"is_ad": true أو false, "arabic_text": "النص المُعاد صياغته هنا، أو فارغ إذا كان إعلاناً"}}
"""

    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4},
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        parsed = json.loads(raw)
        return {
            "is_ad": bool(parsed.get("is_ad", False)),
            "arabic_text": str(parsed.get("arabic_text", "")).strip(),
        }
    except Exception as e:
        log.error(f"فشل استدعاء Gemini: {e} — سيتم نشر النص الأصلي بدون ترجمة كاحتياط.")
        return {"is_ad": False, "arabic_text": text.strip()}


def build_caption(arabic_text: str) -> str:
    if arabic_text:
        return f"{arabic_text}\n\n{CHANNEL_HANDLE_FOR_CAPTION}"
    return CHANNEL_HANDLE_FOR_CAPTION


# ================= النشر عبر Bot API =================
def send_text(text: str):
    resp = requests.post(
        f"{BOT_API_BASE}/sendMessage",
        data={
            "chat_id": TARGET_CHANNEL,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    j = resp.json()
    if not j.get("ok"):
        raise RuntimeError(f"Bot API رفض sendMessage: {j}")


def send_via_bot_api(media_type: str, file_path: str, caption: str):
    endpoint = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[media_type]
    field = {"photo": "photo", "video": "video", "document": "document"}[media_type]
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{BOT_API_BASE}/{endpoint}",
            data={"chat_id": TARGET_CHANNEL, "caption": caption},
            files={field: f},
            timeout=180,
        )
    resp.raise_for_status()
    j = resp.json()
    if not j.get("ok"):
        raise RuntimeError(f"Bot API رفض {endpoint}: {j}")


async def forward_large(msg, caption: str):
    fwd = await client.forward_messages(
        entity=TARGET_CHANNEL,
        messages=msg,
        from_peer=SOURCE_CHANNEL,
        drop_author=True,
    )
    try:
        target_msg = fwd[0] if isinstance(fwd, list) else fwd
        await client.edit_message(TARGET_CHANNEL, target_msg, text=caption, link_preview=False)
    except Exception as e:
        log.warning(f"تعذر تعديل الكابشن بعد forward (الرسالة نشرت بدون تعديل النص): {e}")


async def send_media_or_forward(msg, media_type: str, caption: str):
    size = 0
    try:
        size = msg.file.size or 0
    except Exception:
        pass

    if size and size > MAX_BOT_UPLOAD_BYTES:
        log.info(f"رسالة {msg.id}: الحجم {size} أكبر من الحد — استخدام forward.")
        await forward_large(msg, caption)
        return

    file_path = None
    try:
        file_path = await client.download_media(msg, file="/tmp/relay_download")
        if file_path is None:
            raise RuntimeError("تعذر تحميل الملف")
        send_via_bot_api(media_type, file_path, caption)
    except Exception as e:
        log.warning(f"فشل تحميل/رفع عبر Bot API ({e}) — تجربة forward بدلاً.")
        await forward_large(msg, caption)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


# ================= معالجة رسالة واحدة =================
async def handle_message(msg):
    text = msg.raw_text or ""
    result = gemini_process(text)

    if result["is_ad"]:
        log.info(f"تجاهل إعلان (رسالة {msg.id}).")
        save_last_id(msg.id)
        return

    caption = build_caption(result["arabic_text"])

    try:
        if msg.photo:
            await send_media_or_forward(msg, "photo", caption)
        elif msg.video:
            await send_media_or_forward(msg, "video", caption)
        elif msg.document or msg.audio or msg.voice:
            await send_media_or_forward(msg, "document", caption)
        else:
            send_text(caption)
    except Exception as e:
        log.error(f"فشل نشر الرسالة {msg.id}: {e} — لن يتم تحديث last_id، سيعاد المحاولة لاحقاً.")
        return

    save_last_id(msg.id)
    log.info(f"تم نشر الرسالة {msg.id}.")


# ================= الاستماع المباشر + اللحاق بالفائت =================
@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def live_handler(event):
    try:
        await handle_message(event.message)
    except Exception as e:
        log.error(f"خطأ بمعالجة رسالة حية {event.message.id}: {e}")


async def catch_up():
    last_id = load_last_id()

    if last_id == 0:
        # أول تشغيل على الإطلاق: انشر آخر 5 منشورات فعلياً (بالترتيب الزمني الصحيح)
        messages = await client.get_messages(SOURCE_CHANNEL, limit=5)
        if messages:
            log.info(f"أول تشغيل: نشر آخر {len(messages)} منشورات من قناة المصدر.")
        for msg in reversed(messages):
            await handle_message(msg)
        return

    # التشغيلات اللاحقة: فقط المنشورات الجديدة بعد آخر last_id محفوظ (بدون تكرار)
    messages = await client.get_messages(SOURCE_CHANNEL, min_id=last_id, limit=50)
    if messages:
        log.info(f"اللحاق بـ {len(messages)} رسالة فائتة (جديدة بعد last_id={last_id}).")
    for msg in reversed(messages):
        await handle_message(msg)


async def main():
    await client.start()
    log.info("متصل بتلكرام، بدء اللحاق بالمنشورات الفايتة...")
    await catch_up()
    log.info("بدء الاستماع المباشر لقناة المصدر...")

    start = time.time()
    while time.time() - start < MAX_RUNTIME_SECONDS:
        await asyncio.sleep(30)

    log.info("انتهت مدة التشغيل الآمنة، قطع الاتصال.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
