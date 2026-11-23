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
    "
