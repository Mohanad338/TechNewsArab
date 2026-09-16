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
ALBUM_WAIT_SECONDS = 2.5                   # مهلة انتظار لتجميع بقية صور/فيديوهات نفس الألبوم

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

# تجميع رسائل الألبومات الحية (grouped_id -> list[Message]) قبل معالجتها كوحدة واحدة
_pending_albums = {}
_pending_albums_lock = asyncio.Lock()


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


def send_media_group_via_bot_api(items: list):
    """
    ينشر عدة صور/فيديوهات كمنشور واحد (ألبوم) عبر sendMediaGroup.
    items: قائمة من dict لكل عنصر: {"type": "photo"|"video", "path": file_path}
    الكابشن يُوضع فقط على أول عنصر (Telegram يعرضه كنص المنشور بالألبوم كامل).
    """
    media_payload = []
    files = {}
    open_files = []
    try:
        for i, item in enumerate(items):
            attach_name = f"file{i}"
            f = open(item["path"], "rb")
            open_files.append(f)
            files[attach_name] = f
            entry = {"type": item["type"], "media": f"attach://{attach_name}"}
            if i == 0 and item.get("caption"):
                entry["caption"] = item["caption"]
            media_payload.append(entry)

        resp = requests.post(
            f"{BOT_API_BASE}/sendMediaGroup",
            data={"chat_id": TARGET_CHANNEL, "media": json.dumps(media_payload)},
            files=files,
            timeout=300,
        )
        resp.raise_for_status()
        j = resp.json()
        if not j.get("ok"):
            raise RuntimeError(f"Bot API رفض sendMediaGroup: {j}")
    finally:
        for f in open_files:
            try:
                f.close()
            except Exception:
                pass


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


async def forward_album_large(messages: list, caption: str):
    """فورورد لألبوم كامل دفعة وحدة (لو فيه ملف كبير يتجاوز حد Bot API)."""
    fwd = await client.forward_messages(
        entity=TARGET_CHANNEL,
        messages=messages,
        from_peer=SOURCE_CHANNEL,
        drop_author=True,
    )
    try:
        target_msg = fwd[0] if isinstance(fwd, list) else fwd
        await client.edit_message(TARGET_CHANNEL, target_msg, text=caption, link_preview=False)
    except Exception as e:
        log.warning(f"تعذر تعديل الكابشن بعد forward الألبوم (نشر بدون تعديل النص): {e}")


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


async def send_album_or_forward(messages: list, caption: str):
    """
    يعالج ألبوم (أكثر من صورة/فيديو بمنشور وحد) كوحدة واحدة:
    - لو كل الملفات ضمن حد Bot API: يحمّلها وينشرها دفعة وحدة عبر sendMediaGroup.
    - لو فيه ملف كبير يتجاوز الحد، أو صار خطأ بالتحميل/الرفع: فورورد للألبوم كامل مع drop_author.
    """
    # تحقق من الأحجام أولاً
    total_oversized = False
    for m in messages:
        try:
            if m.file and m.file.size and m.file.size > MAX_BOT_UPLOAD_BYTES:
                total_oversized = True
                break
        except Exception:
            pass

    if total_oversized:
        log.info("ألبوم فيه ملف يتجاوز حد الحجم — استخدام forward للألبوم كامل.")
        await forward_album_large(messages, caption)
        return

    downloaded_paths = []
    try:
        items = []
        for m in messages:
            if m.photo:
                media_type = "photo"
            elif m.video:
                media_type = "video"
            else:
                # لو فيه عنصر بالألبوم مو صورة ولا فيديو (نادر)، أسهل حل آمن: فورورد الألبوم كامل
                raise RuntimeError(f"عنصر بالألبوم برسالة {m.id} ليس صورة ولا فيديو مدعوم بـ sendMediaGroup")

            path = await client.download_media(m, file="/tmp/relay_download")
            if path is None:
                raise RuntimeError(f"تعذر تحميل عنصر الألبوم برسالة {m.id}")
            downloaded_paths.append(path)
            items.append({"type": media_type, "path": path, "caption": caption if len(items) == 0 else None})

        send_media_group_via_bot_api(items)
    except Exception as e:
        log.warning(f"فشل تحميل/رفع الألبوم عبر Bot API ({e}) — تجربة forward للألبوم كامل.")
        await forward_album_large(messages, caption)
    finally:
        for p in downloaded_paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ================= معالجة رسالة واحدة (بدون ألبوم) =================
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


# ================= معالجة ألبوم (أكثر من صورة/فيديو بمنشور واحد) =================
async def handle_album(messages: list):
    """
    messages: كل رسائل الألبوم (لها نفس grouped_id)، بترتيب تصاعدي حسب id.
    ينشرهم كمنشور واحد بالهدف، ويحدّث last_id لأعلى id بالألبوم دفعة وحدة.
    """
    messages = sorted(messages, key=lambda m: m.id)
    max_id = messages[-1].id

    # النص المرافق للألبوم يكون عادة على أحد عناصره (غالباً الأول) - نجمع أي نص موجود
    combined_text = ""
    for m in messages:
        if m.raw_text and m.raw_text.strip():
            combined_text = m.raw_text.strip()
            break

    result = gemini_process(combined_text)

    if result["is_ad"]:
        log.info(f"تجاهل إعلان (ألبوم برسائل {[m.id for m in messages]}).")
        save_last_id(max_id)
        return

    caption = build_caption(result["arabic_text"])

    try:
        # لو ألبوم فيه عناصر غير صور/فيديو (نادر)، send_album_or_forward يرجع forward تلقائياً
        media_messages = [m for m in messages if m.photo or m.video]
        non_media_messages = [m for m in messages if not (m.photo or m.video)]

        if media_messages and not non_media_messages:
            await send_album_or_forward(media_messages, caption)
        else:
            # حالة نادرة: ألبوم فيه مستندات/صوتيات ضمنه - أسلم حل فورورد كامل الألبوم
            log.info(f"ألبوم برسائل {[m.id for m in messages]} فيه عناصر غير مدعومة بـ sendMediaGroup — forward كامل.")
            await forward_album_large(messages, caption)
    except Exception as e:
        log.error(f"فشل نشر الألبوم {[m.id for m in messages]}: {e} — لن يتم تحديث last_id، سيعاد المحاولة لاحقاً.")
        return

    save_last_id(max_id)
    log.info(f"تم نشر الألبوم (رسائل {[m.id for m in messages]}) كمنشور واحد.")


# ================= تجميع رسائل الألبوم الحية قبل المعالجة =================
async def _flush_album_after_delay(grouped_id):
    await asyncio.sleep(ALBUM_WAIT_SECONDS)
    async with _pending_albums_lock:
        messages = _pending_albums.pop(grouped_id, None)
    if not messages:
        return
    try:
        await handle_album(messages)
    except Exception as e:
        log.error(f"خطأ بمعالجة ألبوم حي (grouped_id={grouped_id}): {e}")


# ================= الاستماع المباشر + اللحاق بالفائت =================
@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def live_handler(event):
    msg = event.message
    try:
        if msg.grouped_id:
            # جزء من ألبوم: أضفه لقائمة الانتظار، وابدأ مؤقّت التجميع لأول رسالة بالألبوم فقط
            async with _pending_albums_lock:
                is_first = msg.grouped_id not in _pending_albums
                _pending_albums.setdefault(msg.grouped_id, []).append(msg)
            if is_first:
                asyncio.create_task(_flush_album_after_delay(msg.grouped_id))
        else:
            await handle_message(msg)
    except Exception as e:
        log.error(f"خطأ بمعالجة رسالة حية {msg.id}: {e}")


async def catch_up():
    last_id = load_last_id()

    if last_id == 0:
        # أول تشغيل على الإطلاق: انشر آخر 5 منشورات فعلياً (بالترتيب الزمني الصحيح)
        messages = await client.get_messages(SOURCE_CHANNEL, limit=5)
        if messages:
            log.info(f"أول تشغيل: نشر آخر {len(messages)} منشورات من قناة المصدر.")
        await _process_missed_messages(reversed(messages))
        return

    # التشغيلات اللاحقة: فقط المنشورات الجديدة بعد آخر last_id محفوظ (بدون تكرار)
    messages = await client.get_messages(SOURCE_CHANNEL, min_id=last_id, limit=50)
    if messages:
        log.info(f"اللحاق بـ {len(messages)} رسالة فائتة (جديدة بعد last_id={last_id}).")
    await _process_missed_messages(reversed(messages))


async def _process_missed_messages(messages):
    """
    يعالج رسائل اللحاق بالترتيب الزمني، مع تجميع رسائل نفس الألبوم (grouped_id)
    سوية قبل نشرها كمنشور واحد، بدل معالجة كل رسالة لحالها.
    """
    messages = list(messages)
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.grouped_id:
            group_id = msg.grouped_id
            album_msgs = [msg]
            j = i + 1
            while j < len(messages) and messages[j].grouped_id == group_id:
                album_msgs.append(messages[j])
                j += 1
            await handle_album(album_msgs)
            i = j
        else:
            await handle_message(msg)
            i += 1


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
