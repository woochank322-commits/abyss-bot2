from datetime import datetime
import json
import logging
import os
import re
import threading
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    MemberJoinedEvent,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("abyss_main_bot")

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN과 LINE_CHANNEL_SECRET 환경 변수를 설정해주세요.")

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

KST = ZoneInfo("Asia/Seoul")
RANKING_STORE_PATH = "/tmp/abyss_rankings.json"
TARGETS_STORE_PATH = "/tmp/abyss_targets.json"
# 같은 유저의 연타 도배 방지 최소 간격(초). 0으로 두면 모든 유효 메시지 카운트
MIN_SPEECH_INTERVAL_SEC = 2.0

state_lock = threading.Lock()
last_counted_at: dict[str, float] = {}

WELCOME_MSG = (
    "🌊 ━━━ 🐬 𝓐𝓑𝓨𝓢𝓢 🐬 ━━━ 🌊\n"
    " 새로운 분들의 입장을 환영합니다!\n"
    "𝓐𝓑𝓨𝓢𝓢에 오신 걸 환영해요. 입장 후 간단한 자기소개를 부탁드립니다.\n\n"
    " 기존 멤버분들은 잠시 대화를 멈추고 새로 오신 분의 자기소개를 확인해 주세요. 👀\n"
    " 입장 후 10분 이내 작성 필수 ⚠️ 미완료 시 퇴장될 수 있습니다.\n\n"
    "01. 우측 상단 ≡ 클릭\n"
    "02.  노트 메뉴 입장 \n"
    "03.  공지사항 필독! 끝까지 읽기  댓글에 “확인” 작성 \n"
    "04.  자기소개서 게시글 댓글 작성\n"
    "05.  족보 게시글 댓글 작성\n\n"
    "🦭 모든 노트 작성 완료 후 “노트 작성 완료했습니다!” 라고 말씀해 주세요.\n\n"
    "다시 한번 𝓐𝓑𝓨𝓢𝓢에 오신 것을 환영합니다!"
)

HWAN_MSG = (
    "📜환츄/환찌✉️\n"
    "1.    신입분의 고추나 가슴 사진을 올리기!\n"
    "2.    올렸던 사진을 ‘보내기 취소’\n"
    "3.    그 후 보고싶은 사람을 선택!\n"
    "4.    지목당한 사람은 똑같이 사진을 오픈!\n"
    "5.    (매너 안하면 지목 못해용 )\n\n"
    "📢릴레이 아닙니다.\n"
    "📢신입분들에 한정됩니다."
)


def today_key() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def load_json_dict(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json_dict(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[STORE] failed to save %s: %s", path, e)


ranking_data = load_json_dict(RANKING_STORE_PATH)
targets_data = load_json_dict(TARGETS_STORE_PATH)


def is_valid_counted_message(text: str) -> bool:
    """
    '/'로 시작하지 않으며, 자음/모음 나열(ㅋㅋㅋ, ㅠㅠ 등)이나 기호/이모지가 아닌
    '3글자 이상'의 유효한 단어가 1개 이상 포함된 메시지인지 판별
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return False

    for token in stripped.split():
        cleaned = re.sub(r"[^가-힣A-Za-z0-9]", "", token)
        if len(cleaned) >= 3:
            return True

    return False


def register_chat_target(event):
    """자동 랭킹 전송 대상(group / room / user) 등록"""
    source = getattr(event, "source", None)
    if not source:
        return

    target_id = (
        getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
        or getattr(source, "user_id", None)
    )
    if not target_id:
        return

    with state_lock:
        if target_id not in targets_data:
            targets_data[target_id] = True
            save_json_dict(TARGETS_STORE_PATH, targets_data)
            logger.info("[TARGET] registered chat target=%s", target_id)


def get_chat_targets() -> list[str]:
    with state_lock:
        latest = load_json_dict(TARGETS_STORE_PATH)
        targets_data.update(latest)
        return list(targets_data.keys())


def get_display_name(event) -> str:
    user_id = getattr(event.source, "user_id", None) or "unknown"
    group_id = getattr(event.source, "group_id", None)
    room_id = getattr(event.source, "room_id", None)

    try:
        with ApiClient(configuration) as api_client:
            bot_api = MessagingApi(api_client)
            if group_id and user_id != "unknown":
                profile = bot_api.get_group_member_profile(group_id, user_id)
                if getattr(profile, "display_name", None):
                    return profile.display_name
            elif room_id and user_id != "unknown":
                profile = bot_api.get_room_member_profile(room_id, user_id)
                if getattr(profile, "display_name", None):
                    return profile.display_name
            elif user_id != "unknown":
                profile = bot_api.get_profile(user_id)
                if getattr(profile, "display_name", None):
                    return profile.display_name
    except Exception:
        pass

    return user_id[-6:] if user_id != "unknown" else "익명"


def add_speech_count(user_id: str, name: str, amount: int = 1) -> int:
    """오늘 날짜(KST) 기준 유효 발화 횟수 적립"""
    day = today_key()
    with state_lock:
        day_board = ranking_data.setdefault(day, {})
        entry = day_board.setdefault(user_id, {"name": name, "madit": 0})
        if name:
            entry["name"] = name
        entry["madit"] = int(entry.get("madit", 0)) + amount
        save_json_dict(RANKING_STORE_PATH, ranking_data)
        return entry["madit"]


def get_sorted_today_entries() -> list[tuple[str, dict]]:
    day = today_key()
    with state_lock:
        latest = load_json_dict(RANKING_STORE_PATH)
        ranking_data.update(latest)
        day_board = ranking_data.get(day, {})
        items = list(day_board.items())
    items.sort(key=lambda kv: int(kv[1].get("madit", 0)), reverse=True)
    return items


def format_my_ranking(user_id: str | None, fallback_name: str) -> str:
    day = today_key()
    sorted_items = get_sorted_today_entries()

    if not user_id or not sorted_items:
        return (
            f"📊 [{day}] 내 랭킹\n"
            f"• 닉네임: {fallback_name}\n"
            f"• 오늘 발언 수: 0마디\n"
            f"• 오늘 현재 순위: 기록 없음"
        )

    rank = None
    my_count = 0
    my_name = fallback_name

    for idx, (uid, info) in enumerate(sorted_items, start=1):
        if uid == user_id:
            rank = idx
            my_count = int(info.get("madit", 0))
            my_name = info.get("name") or fallback_name
            break

    if rank is None:
        return (
            f"📊 [{day}] 내 랭킹\n"
            f"• 닉네임: {my_name}\n"
            f"• 오늘 발언 수: 0마디\n"
            f"• 오늘 현재 순위: 기록 없음"
        )

    return (
        f"📊 [{day}] 내 랭킹\n"
        f"• 닉네임: {my_name}\n"
        f"• 오늘 발언 수: {my_count}마디\n"
        f"• 오늘 현재 순위: {rank}위 / {len(sorted_items)}명"
    )


def format_all_ranking(header_prefix: str = "🏆") -> str:
    day = today_key()
    sorted_items = get_sorted_today_entries()

    if not sorted_items:
        return f"{header_prefix} [{day}] 전체 랭킹\n아직 오늘 기록된 발언이 없습니다."

    lines = [f"{header_prefix} [{day}] 전체 랭킹"]
    for idx, (_, info) in enumerate(sorted_items, start=1):
        name = info.get("name") or "멤버"
        count = int(info.get("madit", 0))
        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"{idx}."))
        lines.append(f"{medal} {name} — {count}마디")

    return "\n".join(lines)


def reply_messages(reply_token: str, texts: list[str]):
    messages = [TextMessage(text=t) for t in texts[:5]]
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages,
            )
        )


def push_hourly_ranking():
    targets = get_chat_targets()
    if not targets:
        logger.info("[HOURLY_RANKING] skipped: no registered targets yet")
        return

    ranking_text = format_all_ranking(header_prefix="⏰")
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for target_id in targets:
            try:
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=target_id,
                        messages=[TextMessage(text=ranking_text)],
                    )
                )
                logger.info("[HOURLY_RANKING] pushed to %s", target_id)
            except Exception as e:
                logger.warning("[HOURLY_RANKING] failed to push to %s: %s", target_id, e)


def hourly_ranking_scheduler_loop():
    last_sent_slot = None
    while True:
        now_kst = datetime.now(KST)
        slot_key = now_kst.strftime("%Y-%m-%d %H:%M")
        if now_kst.minute == 0 and slot_key != last_sent_slot:
            last_sent_slot = slot_key
            push_hourly_ranking()
        time.sleep(15)


_scheduler_started = False
_scheduler_lock = threading.Lock()


def ensure_scheduler_started():
    global _scheduler_started
    with _scheduler_lock:
        if not _scheduler_started:
            t = threading.Thread(target=hourly_ranking_scheduler_loop, daemon=True)
            t.start()
            _scheduler_started = True
            logger.info("[SCHEDULER] hourly ranking scheduler started")


ensure_scheduler_started()


@app.route("/", methods=["GET"])
def health_check():
    ensure_scheduler_started()
    return "ABYSS Main Room Bot is running.", 200


@app.route("/callback", methods=["POST"])
def callback():
    ensure_scheduler_started()
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MemberJoinedEvent)
def handle_member_joined(event):
    register_chat_target(event)
    reply_messages(event.reply_token, [WELCOME_MSG])


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    register_chat_target(event)
    user_text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)

    if user_text in ("/환찌", "/환츄"):
        reply_messages(event.reply_token, [HWAN_MSG])
        return

    if user_text == "/내랭킹":
        name = get_display_name(event)
        reply_messages(event.reply_token, [format_my_ranking(user_id, name)])
        return

    if user_text == "/전체랭킹":
        reply_messages(event.reply_token, [format_all_ranking()])
        return

    if user_id and is_valid_counted_message(user_text):
        now_ts = time.monotonic()
        with state_lock:
            prev_ts = last_counted_at.get(user_id, 0.0)
            if MIN_SPEECH_INTERVAL_SEC > 0 and (now_ts - prev_ts) < MIN_SPEECH_INTERVAL_SEC:
                logger.info("[SPEECH] rate-limited user_id=%s text=%r", user_id, user_text)
                return
            last_counted_at[user_id] = now_ts

        name = get_display_name(event)
        total = add_speech_count(user_id, name, 1)
        logger.info("[SPEECH] counted user_id=%s name=%s total=%d text=%r", user_id, name, total, user_text)


if __name__ == "__main__":
    ensure_scheduler_started()
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
