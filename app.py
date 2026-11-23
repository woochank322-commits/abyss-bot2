from datetime import datetime
import json
import logging
import os
import re
import tempfile
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
    MemberLeftEvent,
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
MEMBERS_STORE_PATH = "/tmp/abyss_members.json"
# 같은 유저의 연타 도배 방지 최소 간격(초). 0으로 두면 모든 유효 메시지 카운트
MIN_SPEECH_INTERVAL_SEC = 2.0
# 일일 활동부족 경고 기준 마디 수 (30마디 미만 시 경고 표시)
MIN_ACTIVITY_MADIT = 30
# 1개 메시지에서 인정되는 유효 단어(3글자 이상)의 최소 길이
MIN_WORD_LEN = 3

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
    except Exception as e:
        logger.warning("[STORE] failed to load %s: %s", path, e)
        return {}


def save_json_dict(path: str, data: dict):
    """임시 파일 기록 후 os.replace()로 원자적 저장"""
    directory = os.path.dirname(path) or "/tmp"
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("[STORE] failed to save %s: %s", path, e)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


ranking_data = load_json_dict(RANKING_STORE_PATH)
targets_data = load_json_dict(TARGETS_STORE_PATH)
members_data = load_json_dict(MEMBERS_STORE_PATH)


def normalize_command(text: str) -> str:
    """앞뒤 공백 및 @멘션 토큰 제거 후 첫 명령어 토큰 반환"""
    tokens = [tok for tok in text.strip().split() if not tok.startswith("@")]
    return tokens[0] if tokens else ""


def calculate_message_madit(text: str) -> int:
    """
    '/'로 시작하지 않는 메시지에서 한글·영문·숫자 기준 3글자 이상인
    유효 단어의 개수를 마디 수로 계산 (없으면 0 반환)
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return 0

    count = 0
    for token in stripped.split():
        if token.startswith("@"):
            continue
        cleaned = re.sub(r"[^가-힣A-Za-z0-9]", "", token)
        if len(cleaned) >= MIN_WORD_LEN:
            count += 1

    return count


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
        latest = load_json_dict(TARGETS_STORE_PATH)
        targets_data.update(latest)
        if target_id not in targets_data:
            targets_data[target_id] = True
            save_json_dict(TARGETS_STORE_PATH, targets_data)
            logger.info("[TARGET] registered chat target=%s", target_id)


def get_chat_targets() -> list[str]:
    with state_lock:
        latest = load_json_dict(TARGETS_STORE_PATH)
        targets_data.update(latest)
        return list(targets_data.keys())


def get_display_name_by_ids(
    user_id: str | None,
    group_id: str | None = None,
    room_id: str | None = None,
) -> str:
    """외부 LINE Profile API 호출 — 반드시 state_lock 밖에서 호출"""
    uid = user_id or "unknown"
    try:
        with ApiClient(configuration) as api_client:
            bot_api = MessagingApi(api_client)
            if group_id and uid != "unknown":
                profile = bot_api.get_group_member_profile(group_id, uid)
                if getattr(profile, "display_name", None):
                    return profile.display_name
            elif room_id and uid != "unknown":
                profile = bot_api.get_room_member_profile(room_id, uid)
                if getattr(profile, "display_name", None):
                    return profile.display_name
            elif uid != "unknown":
                profile = bot_api.get_profile(uid)
                if getattr(profile, "display_name", None):
                    return profile.display_name
    except Exception as e:
        logger.debug("[PROFILE] fallback for user_id=%s: %s", uid, e)

    return uid[-6:] if uid != "unknown" else "익명"


def get_display_name(event) -> str:
    """이벤트로부터 발신자 닉네임 조회 — 반드시 state_lock 밖에서 호출"""
    source = getattr(event, "source", None)
    user_id = getattr(source, "user_id", None)
    group_id = getattr(source, "group_id", None)
    room_id = getattr(source, "room_id", None)
    return get_display_name_by_ids(user_id=user_id, group_id=group_id, room_id=room_id)


def upsert_member(user_id: str | None, name: str | None = None):
    """방에서 인지된 멤버를 상시 멤버 목록에 등록/갱신"""
    if not user_id:
        return
    with state_lock:
        latest = load_json_dict(MEMBERS_STORE_PATH)
        members_data.update(latest)
        existing = members_data.get(user_id, {})
        resolved_name = name or existing.get("name") or user_id[-6:]
        members_data[user_id] = {
            "name": resolved_name,
            "updated_at": datetime.now(KST).isoformat(),
        }
        save_json_dict(MEMBERS_STORE_PATH, members_data)


def remove_member(user_id: str | None, remove_today_ranking: bool = True):
    """퇴장한 멤버를 상시 멤버 목록 및 오늘 랭킹에서 자동 제외"""
    if not user_id:
        return
    day = today_key()
    with state_lock:
        latest_members = load_json_dict(MEMBERS_STORE_PATH)
        members_data.clear()
        members_data.update(latest_members)
        removed_name = (members_data.pop(user_id, {}) or {}).get("name")
        members_changed = removed_name is not None or user_id in latest_members
        if members_changed:
            save_json_dict(MEMBERS_STORE_PATH, members_data)

        ranking_changed = False
        if remove_today_ranking:
            latest_rank = load_json_dict(RANKING_STORE_PATH)
            ranking_data.clear()
            ranking_data.update(latest_rank)
            day_board = ranking_data.get(day)
            if isinstance(day_board, dict) and user_id in day_board:
                rank_info = day_board.pop(user_id, None)
                if not removed_name and isinstance(rank_info, dict):
                    removed_name = rank_info.get("name")
                ranking_changed = True
                save_json_dict(RANKING_STORE_PATH, ranking_data)

        if members_changed or ranking_changed:
            logger.info(
                "[MEMBER_LEFT] removed user_id=%s name=%s (members=%s, today_ranking=%s)",
                user_id,
                removed_name or user_id[-6:],
                members_changed,
                ranking_changed,
            )


def sync_group_members_from_api():
    """가능한 경우 그룹 멤버 ID 목록을 조회해 미등록 멤버(0마디)도 보충"""
    targets = get_chat_targets()
    group_ids = [tid for tid in targets if isinstance(tid, str) and tid.startswith("C")]
    if not group_ids:
        return

    with state_lock:
        latest = load_json_dict(MEMBERS_STORE_PATH)
        members_data.update(latest)
        known_ids = set(members_data.keys())

    for gid in group_ids:
        start_token = None
        while True:
            try:
                with ApiClient(configuration) as api_client:
                    bot_api = MessagingApi(api_client)
                    res = bot_api.get_group_members_ids(gid, start=start_token)
            except Exception as e:
                logger.debug("[MEMBERS_SYNC] skipped get_group_members_ids for %s: %s", gid, e)
                break

            member_ids = getattr(res, "member_ids", None) or []
            for uid in member_ids:
                if uid and uid not in known_ids:
                    display_name = get_display_name_by_ids(uid, group_id=gid)
                    upsert_member(uid, display_name)
                    known_ids.add(uid)

            start_token = getattr(res, "next", None)
            if not start_token:
                break


def ensure_known_member(user_id: str | None, name: str):
    """오늘 기록이 없더라도 알려진 멤버로 등록해 0마디 미활동자도 랭킹에 포함"""
    if not user_id:
        return
    upsert_member(user_id, name)
    day = today_key()
    with state_lock:
        latest = load_json_dict(RANKING_STORE_PATH)
        ranking_data.update(latest)
        day_board = ranking_data.setdefault(day, {})
        if user_id not in day_board:
            day_board[user_id] = {"name": name or user_id[-6:], "madit": 0}
            save_json_dict(RANKING_STORE_PATH, ranking_data)
        elif name:
            day_board[user_id]["name"] = name
            save_json_dict(RANKING_STORE_PATH, ranking_data)


def add_speech_count(user_id: str, name: str, amount: int = 1) -> int:
    """이미 조회된 name을 받아 state_lock 안에서만 상태 갱신 및 파일 저장"""
    upsert_member(user_id, name)
    day = today_key()
    with state_lock:
        latest = load_json_dict(RANKING_STORE_PATH)
        ranking_data.update(latest)
        day_board = ranking_data.setdefault(day, {})
        entry = day_board.setdefault(user_id, {"name": name, "madit": 0})
        if name:
            entry["name"] = name
        entry["madit"] = int(entry.get("madit", 0)) + amount
        save_json_dict(RANKING_STORE_PATH, ranking_data)
        return entry["madit"]


def get_sorted_today_entries() -> list[tuple[str, dict]]:
    """
    현재 방에 남아 있는 멤버(오늘 0마디 포함) 기준으로 오늘 발언 수 내림차순 정렬
    """
    day = today_key()
    with state_lock:
        latest_rank = load_json_dict(RANKING_STORE_PATH)
        ranking_data.update(latest_rank)

        latest_members = load_json_dict(MEMBERS_STORE_PATH)
        members_data.clear()
        members_data.update(latest_members)

        merged_today: dict[str, dict] = {}

        for uid, m_info in members_data.items():
            if not uid or not isinstance(m_info, dict):
                continue
            merged_today[uid] = {
                "name": m_info.get("name") or uid[-6:],
                "madit": 0,
            }

        day_board = ranking_data.get(day, {})
