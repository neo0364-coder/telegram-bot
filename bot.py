import os
import logging
import asyncio
import random
import json
import threading
import time
import base64
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request
from telegram import Update, Bot
from groq import Groq
from tavily import TavilyClient

# ─── solders / solana-py ──────────────────────────────────────────
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
import httpx

logging.basicConfig(level=logging.INFO)

# ─── 타임존 ────────────────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")

# ─── 환경변수 ─────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
WEBHOOK_URL     = os.environ["WEBHOOK_URL"].rstrip("/")
TAVILY_API_KEY  = os.environ["TAVILY_API_KEY"]

RPC_URL         = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
BOT_PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY2"]
TOKEN_MINT      = os.environ.get("TOKEN_MINT", "HjaXEmHxwWh42nA9vYb5rDmtviNFompy3nn1zpSh4wvK")

EXTRA_WALLET_KEYS = []
for i in range(1, 11):
    key = os.environ.get(f"WALLET_{i}_KEY")
    if key:
        EXTRA_WALLET_KEYS.append((i, key))

LARGE_WALLET_INDEX      = 5
EXCLUDE_WALLET_INDEXES  = [4]
LARGE_WALLET_MULTIPLIER = 1.5

# ─── 지갑1 전용 거래 간격 (2시간 근처 랜덤, ±20분) ─────────────────
WALLET1_WAIT_MIN_SEC = 100 * 60   # 100분
WALLET1_WAIT_MAX_SEC = 140 * 60   # 140분

# ─── Jupiter Ultra API (jup.ag 웹사이트가 실제로 사용하는 엔진) ──────
# 기존 swap/v1 (Metis 라우팅 엔진)은 신규/저유동성 토큰에 대해 TOKEN_NOT_TRADABLE을
# 반환하는 인덱싱 지연 문제가 있음. Ultra API는 "Just-In-Time Market Revival" 기능으로
# 이런 마켓을 동적으로 재인덱싱하므로 jup.ag 웹사이트에서는 거래가 가능했던 것.
WSOL_MINT = "So11111111111111111111111111111111111111112"

# API 키는 반드시 환경변수로 관리 (Railway Variables에 JUPITER_API_KEY로 등록)
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "")
JUPITER_HEADERS = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}

# api.jup.ag(키 인증) 우선, lite-api.jup.ag(무료 티어)를 폴백으로 사용
JUPITER_ORDER_ENDPOINTS = [
    "https://api.jup.ag/ultra/v1/order",
    "https://lite-api.jup.ag/ultra/v1/order",
]
JUPITER_EXECUTE_ENDPOINTS = [
    "https://api.jup.ag/ultra/v1/execute",
    "https://lite-api.jup.ag/ultra/v1/execute",
]

# 토큰이 방금 막 인덱싱되었거나 일시적으로 라우팅에서 빠진 경우를 대비한 재시도 설정
NOT_TRADABLE_RETRY_COUNT = 2
NOT_TRADABLE_RETRY_WAIT_SEC = 15

# ─── 환경변수로 관리되는 설정 ─────────────────────────────────────
MIN_SOL            = float(os.environ.get("MIN_SOL", "0.0005"))
MAX_SOL            = float(os.environ.get("MAX_SOL", "0.001"))
SLIPPAGE_BPS       = int(os.environ.get("SLIPPAGE_BPS", "30"))
PRIORITY_FEE_MICRO = int(os.environ.get("PRIORITY_FEE_MICRO", "5000"))
TOKEN_DECIMALS     = int(os.environ.get("TOKEN_DECIMALS", "6"))

# ─── 스레드 안전 상태 관리 ────────────────────────────────────────
_state_lock           = threading.Lock()
trading_active        = False
trading_threads       = []
daily_log             = []
conversation_history  = {}
alert_chat_id         = None
MAX_CONSECUTIVE_FAILS = 3

def set_trading(val: bool):
    global trading_active
    with _state_lock:
        trading_active = val

def is_trading() -> bool:
    with _state_lock:
        return trading_active

# ─── 키 파싱 유틸 ─────────────────────────────────────────────────
def parse_keypair(raw: str) -> Keypair:
    raw = raw.strip().strip('"').strip("'")
    if raw.startswith("0x") or raw.startswith("0X"):
        raw = raw[2:]
    if raw.startswith("["):
        arr = json.loads(raw)
        return Keypair.from_bytes(bytes(arr))
    else:
        return Keypair.from_base58_string(raw)

# ─── 봇/지갑 초기화 ───────────────────────────────────────────────
BOT_KEYPAIR = parse_keypair(BOT_PRIVATE_KEY)
BOT_ADDRESS = str(BOT_KEYPAIR.pubkey())

groq_client   = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
app           = Flask(__name__)

SEARCH_KEYWORDS = ["현재","지금","오늘","최신","최근","주가","날씨","뉴스","환율","가격","몇시","누구야","대통령","총리","결과"]

def needs_search(text):
    return any(k in text for k in SEARCH_KEYWORDS)

# ─── 자동 중지 + 텔레그램 알림 ───────────────────────────────────
def auto_stop(reason: str):
    set_trading(False)
    logging.error(f"자동 중지: {reason}")
    if alert_chat_id:
        def _send():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _inner():
                async with Bot(token=TELEGRAM_TOKEN) as bot:
                    await bot.send_message(
                        chat_id=alert_chat_id,
                        text=f"🚨 자동거래 자동 중지!\n\n사유: {reason}\n\n/starttrading 으로 재시작 가능합니다."
                    )
            try:
                loop.run_until_complete(_inner())
            finally:
                loop.close()
        threading.Thread(target=_send, daemon=True).start()

# ─── RPC 유틸 ─────────────────────────────────────────────────────
def rpc_post(method: str, params: list):
    resp = httpx.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC 오류: {data['error']}")
    return data["result"]

def get_sol_balance(pubkey: str) -> float:
    result = rpc_post("getBalance", [pubkey, {"commitment": "confirmed"}])
    return result["value"] / 1e9

def get_token_balance(pubkey: str, mint: str) -> int:
    result = rpc_post("getTokenAccountsByOwner", [
        pubkey,
        {"mint": mint},
        {"encoding": "jsonParsed", "commitment": "confirmed"}
    ])
    accounts = result.get("value", [])
    if not accounts:
        return 0
    info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
    return int(info["amount"])

def send_transaction(signed_tx_b64: str) -> str:
    result = rpc_post("sendTransaction", [
        signed_tx_b64,
        {"encoding": "base64", "preflightCommitment": "confirmed", "skipPreflight": False}
    ])
    return result

def confirm_transaction(sig: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = rpc_post("getSignatureStatuses", [[sig]])
        status = result["value"][0]
        if status is not None:
            if status.get("err"):
                return False
            conf = status.get("confirmationStatus", "")
            if conf in ("confirmed", "finalized"):
                return True
        time.sleep(2)
    return False

# ─── Jupiter Ultra: Order 요청 (quote+tx 생성이 한 번에 처리됨) ───
def jupiter_ultra_order(input_mint: str, output_mint: str, amount_lamports: int, taker_pubkey: str) -> dict:
    last_exc = None
    for url in JUPITER_ORDER_ENDPOINTS:
        not_tradable_attempt = 0
        while not_tradable_attempt <= NOT_TRADABLE_RETRY_COUNT:
            got_not_tradable = False
            for attempt in range(3):
                try:
                    resp = httpx.get(url, params={
                        "inputMint":  input_mint,
                        "outputMint": output_mint,
                        "amount":     amount_lamports,
                        "taker":      taker_pubkey,
                    }, headers=JUPITER_HEADERS, timeout=15)
                    logging.info(f"Jupiter order [{url}] 응답: {resp.status_code} {resp.text[:300]}")
                    resp.raise_for_status()
                    data = resp.json()
                    # transaction이 null이면 라우팅 가능한 경로가 없다는 뜻 (구버전 TOKEN_NOT_TRADABLE과 동일 취급)
                    if not data.get("transaction"):
                        got_not_tradable = True
                        last_exc = RuntimeError(f"Ultra order: no route/transaction returned ({data.get('errorMessage', data)})")
                        logging.warning(
                            f"Jupiter order 라우팅 경로 없음 [{url}] "
                            f"(재시도 {not_tradable_attempt+1}/{NOT_TRADABLE_RETRY_COUNT+1})"
                        )
                        break
                    return data
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    last_exc = e
                    logging.warning(f"Jupiter order 실패 [{url}] ({attempt+1}/3): {e}")
                    time.sleep(5 * (attempt + 1))
                except httpx.HTTPStatusError as e:
                    last_exc = e
                    if e.response.status_code == 400 and "TOKEN_NOT_TRADABLE" in e.response.text:
                        got_not_tradable = True
                        logging.warning(
                            f"Jupiter order TOKEN_NOT_TRADABLE [{url}] "
                            f"(재시도 {not_tradable_attempt+1}/{NOT_TRADABLE_RETRY_COUNT+1}): {e}"
                        )
                    else:
                        logging.warning(f"Jupiter order HTTP 오류 [{url}]: {e}")
                    break

            if not got_not_tradable:
                break
            not_tradable_attempt += 1
            if not_tradable_attempt <= NOT_TRADABLE_RETRY_COUNT:
                time.sleep(NOT_TRADABLE_RETRY_WAIT_SEC)
    raise RuntimeError(f"Jupiter order 모든 엔드포인트 실패: {last_exc}")

# ─── Jupiter Ultra: 서명된 트랜잭션 실행 (RPC 직접 전송 대신 Jupiter 인프라가 처리) ──
def jupiter_ultra_execute(signed_tx_b64: str, request_id: str) -> dict:
    last_exc = None
    for url in JUPITER_EXECUTE_ENDPOINTS:
        for attempt in range(3):
            try:
                resp = httpx.post(url, json={
                    "signedTransaction": signed_tx_b64,
                    "requestId":         request_id,
                }, headers=JUPITER_HEADERS, timeout=30)
                logging.info(f"Jupiter execute [{url}] 응답: {resp.status_code} {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exc = e
                logging.warning(f"Jupiter execute 실패 [{url}] ({attempt+1}/3): {e}")
                time.sleep(5 * (attempt + 1))
            except httpx.HTTPStatusError as e:
                last_exc = e
                logging.warning(f"Jupiter execute HTTP 오류 [{url}]: {e}")
                break
    raise RuntimeError(f"Jupiter execute 모든 엔드포인트 실패: {last_exc}")

# ─── 서명 (Ultra는 전송을 자체 인프라가 처리하므로 서명까지만 담당) ──
def sign_transaction(tx_b64: str, keypair: Keypair) -> str:
    raw = base64.b64decode(tx_b64)
    tx  = VersionedTransaction.from_bytes(raw)

    msg_bytes = to_bytes_versioned(tx.message)
    my_sig    = keypair.sign_message(msg_bytes)

    sigs         = list(tx.signatures)
    account_keys = tx.message.account_keys
    my_pubkey    = keypair.pubkey()
    for i, key in enumerate(account_keys):
        if str(key) == str(my_pubkey) and i < len(sigs):
            sigs[i] = my_sig
            break
    else:
        if sigs:
            sigs[0] = my_sig
        else:
            sigs = [my_sig]

    signed     = VersionedTransaction.populate(tx.message, sigs)
    signed_b64 = base64.b64encode(bytes(signed)).decode()
    return signed_b64

# ─── Ultra 주문 실행 헬퍼: order → 서명 → execute → 결과 확인 ──────
def run_ultra_swap(input_mint: str, output_mint: str, amount_lamports: int, keypair: Keypair) -> tuple[str, bool, int]:
    pubkey = str(keypair.pubkey())
    order  = jupiter_ultra_order(input_mint, output_mint, amount_lamports, pubkey)

    expected     = int(order.get("outAmount", 0))
    request_id   = order["requestId"]
    signed_tx    = sign_transaction(order["transaction"], keypair)
    result       = jupiter_ultra_execute(signed_tx, request_id)

    status = result.get("status")
    sig    = result.get("signature")
    ok     = (status == "Success")
    if not ok:
        logging.warning(f"Ultra execute 실패 상태: {status}, 상세: {result.get('error') or result}")
    return sig, ok, expected

# ─── 매수 (SOL → TOKEN) ───────────────────────────────────────────
def buy_token(keypair: Keypair, multiplier: float = 1.0):
    try:
        pubkey     = str(keypair.pubkey())
        sol_amount = random.uniform(MIN_SOL, MAX_SOL) * multiplier
        lamports   = int(sol_amount * 1e9)

        sol_bal = get_sol_balance(pubkey)
        if sol_bal < sol_amount + 0.005:
            logging.warning(f"[{pubkey[:8]}] SOL 잔액 부족: {sol_bal:.4f}")
            return None, False, f"SOL 잔액 부족 ({sol_bal:.4f})", 0

        logging.info(f"[{pubkey[:8]}] 매수 시도: {sol_amount:.4f} SOL")

        sig, ok, expected = run_ultra_swap(WSOL_MINT, TOKEN_MINT, lamports, keypair)

        received = expected if ok else 0
        if ok:
            logging.info(f"[{pubkey[:8]}] 매수 성공 — {received} raw token 수령(예상치)")

        logging.info(f"[{pubkey[:8]}] 매수 결과: {'성공' if ok else '실패'} sig={sig}")
        return sig, ok, f"{sol_amount:.4f} SOL", received

    except Exception as e:
        logging.error(f"매수 오류 ({str(keypair.pubkey())[:8]}): {e}", exc_info=True)
        return None, False, str(e)[:100], 0

# ─── 매도 (TOKEN → SOL) ───────────────────────────────────────────
def sell_token(keypair: Keypair, received_amount: int = 0, multiplier: float = 1.0):
    try:
        pubkey  = str(keypair.pubkey())
        balance = get_token_balance(pubkey, TOKEN_MINT)
        if balance == 0:
            logging.warning(f"[{pubkey[:8]}] 토큰 잔액 없음")
            return None, False, "토큰 잔액없음"

        if received_amount > 0:
            sell_ratio = random.uniform(0.90, 1.00)
            amount_in  = min(int(received_amount * sell_ratio * multiplier), balance)
            logging.info(f"[{pubkey[:8]}] 매도 비율: {sell_ratio:.2%} of {received_amount}")
        else:
            amount_in = max(1, int(balance * 0.01))

        amount_in = min(amount_in, balance)

        if amount_in == 0:
            return None, False, "매도량 0"

        logging.info(f"[{pubkey[:8]}] 매도 시도: {amount_in} raw token")
        sig, ok, _ = run_ultra_swap(TOKEN_MINT, WSOL_MINT, amount_in, keypair)

        readable = amount_in / (10 ** TOKEN_DECIMALS)
        logging.info(f"[{pubkey[:8]}] 매도 결과: {'성공' if ok else '실패'} sig={sig}")
        return sig, ok, f"{readable:.4f} TOKEN"

    except Exception as e:
        logging.error(f"매도 오류 ({str(keypair.pubkey())[:8]}): {e}", exc_info=True)
        return None, False, str(e)[:100]

# ─── 로그 기록 헬퍼 ──────────────────────────────────────────────
def log_trade(label: str, action: str, sig, ok: bool, info: str):
    ts  = datetime.now(KST).strftime("%H:%M")
    tag = "✅" if ok else "❌"
    daily_log.append(f"{tag} [{label}] {ts} {action} {info}")
    if sig:
        daily_log.append(f"   └ https://solscan.io/tx/{sig}")

# ─── 대기 헬퍼 (중단 가능) ───────────────────────────────────────
def interruptible_sleep(seconds: int) -> bool:
    for _ in range(seconds):
        if not is_trading():
            return False
        time.sleep(1)
    return True

# ─── 메인 지갑 루프 ───────────────────────────────────────────────
def main_wallet_loop(keypair: Keypair):
    label          = "메인"
    buy_count      = 0
    fail_count     = 0
    pending_tokens = 0
    logging.info(f"[{label}] 거래 루프 시작 — 즉시 첫 매수 (매수2:매도1)")

    while is_trading():
        try:
            sol_bal = get_sol_balance(str(keypair.pubkey()))
            if sol_bal < 0.002:
                auto_stop(f"[{label}] SOL 잔액 부족 ({sol_bal:.4f} SOL)")
                return

            if buy_count < 2:
                sig, ok, info, received = buy_token(keypair)
                log_trade(label, "매수", sig, ok, info)
                if ok:
                    buy_count      += 1
                    fail_count      = 0
                    pending_tokens += received
                    logging.info(f"[{label}] 매수 {buy_count}/2 — 누적 미매도 {pending_tokens}")
                else:
                    fail_count += 1
            else:
                sig, ok, info = sell_token(keypair, received_amount=pending_tokens)
                log_trade(label, "매도", sig, ok, info)
                if ok:
                    buy_count      = 0
                    fail_count     = 0
                    pending_tokens = 0
                    logging.info(f"[{label}] 매도 완료 — 카운트 초기화")
                else:
                    fail_count += 1

            if fail_count >= MAX_CONSECUTIVE_FAILS:
                auto_stop(f"[{label}] 연속 {fail_count}회 거래 실패")
                return

            wait = random.randint(45 * 60, 75 * 60)
            logging.info(f"[{label}] 다음 거래까지 {wait//60}분 대기")
            if not interruptible_sleep(wait):
                return

        except Exception as e:
            fail_count += 1
            logging.error(f"[{label}] 루프 오류 ({fail_count}/{MAX_CONSECUTIVE_FAILS}): {e}", exc_info=True)
            if fail_count >= MAX_CONSECUTIVE_FAILS:
                auto_stop(f"[{label}] 연속 {fail_count}회 오류: {str(e)[:80]}")
                return
            time.sleep(60)

# ─── 추가 지갑 루프 ───────────────────────────────────────────────
# wait_min_sec / wait_max_sec 을 지정하면 해당 지갑만 다른 거래 간격을 쓸 수 있음
# (기본값은 기존과 동일한 4~6시간)
def extra_wallet_loop(keypair: Keypair, wallet_label: str, multiplier: float = 1.0,
                       wait_min_sec: int = 240 * 60, wait_max_sec: int = 360 * 60):
    initial_delay = random.randint(0, 6 * 3600)
    logging.info(f"[{wallet_label}] 거래 루프 시작 — 초기 대기 {initial_delay//60}분")
    if not interruptible_sleep(initial_delay):
        return

    next_action   = "buy"
    fail_count    = 0
    last_received = 0

    while is_trading():
        try:
            sol_bal = get_sol_balance(str(keypair.pubkey()))
            if sol_bal < 0.002:
                logging.warning(f"[{wallet_label}] SOL 잔액 부족 ({sol_bal:.4f}) — 거래 중단")
                daily_log.append(f"⚠️ [{wallet_label}] SOL 부족으로 거래 중단")
                return

            if next_action == "buy":
                sig, ok, info, received = buy_token(keypair, multiplier)
                log_trade(wallet_label, "매수", sig, ok, info)
                if ok:
                    next_action   = "sell"
                    fail_count    = 0
                    last_received = received
                else:
                    fail_count += 1
            else:
                sig, ok, info = sell_token(keypair, received_amount=last_received, multiplier=multiplier)
                log_trade(wallet_label, "매도", sig, ok, info)
                if ok:
                    next_action   = "buy"
                    fail_count    = 0
                    last_received = 0
                else:
                    fail_count += 1

            if fail_count >= MAX_CONSECUTIVE_FAILS:
                logging.warning(f"[{wallet_label}] 연속 {fail_count}회 실패 — 거래 중단")
                daily_log.append(f"⚠️ [{wallet_label}] 연속 {fail_count}회 실패로 거래 중단")
                return

            wait = random.randint(wait_min_sec, wait_max_sec)
            logging.info(f"[{wallet_label}] 다음 거래까지 {wait//60}분 대기")
            if not interruptible_sleep(wait):
                return

        except Exception as e:
            fail_count += 1
            logging.error(f"[{wallet_label}] 루프 오류 ({fail_count}/{MAX_CONSECUTIVE_FAILS}): {e}", exc_info=True)
            if fail_count >= MAX_CONSECUTIVE_FAILS:
                logging.warning(f"[{wallet_label}] 연속 오류로 거래 중단")
                return
            time.sleep(60)

# ─── 하루 5회 보고 스케줄러 ───────────────────────────────────────
def daily_report_loop(chat_id):
    def send_report():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _inner():
                async with Bot(token=TELEGRAM_TOKEN) as bot:
                    if not daily_log:
                        msg = "📊 일일 리포트\n오늘 거래 내역이 없습니다."
                    else:
                        msg = f"📊 일일 리포트 ({len(daily_log)}건)\n\n" + "\n".join(daily_log[-50:])
                    await bot.send_message(chat_id=chat_id, text=msg[:4000])
                    daily_log.clear()
            try:
                loop.run_until_complete(_inner())
            finally:
                loop.close()
        except Exception as e:
            # 리포트 전송이 실패해도 스케줄러 루프 자체는 죽지 않도록 여기서 잡아준다
            logging.error(f"[daily_report_loop] 리포트 전송 실패: {e}", exc_info=True)

    while is_trading():
        now          = datetime.now(KST)
        # 오름차순 정렬 필수 (wrap-around 계산 로직이 정렬을 전제로 함)
        target_hours = [1, 9, 13, 17, 21]
        next_hour    = next((h for h in target_hours if h > now.hour), None)
        if next_hour:
            wait = (next_hour - now.hour) * 3600 - now.minute * 60
        else:
            wait = (24 - now.hour + target_hours[0]) * 3600 - now.minute * 60
        wait = max(wait, 60)

        if not interruptible_sleep(min(wait, 3600)):
            return

        now2 = datetime.now(KST)
        if now2.hour in target_hours and now2.minute < 5:
            send_report()
            time.sleep(300)

# ─── 텔레그램 핸들러 ──────────────────────────────────────────────
async def handle_update(update_data):
    global trading_threads, alert_chat_id
    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        update = Update.de_json(update_data, bot)
        if not update.message or not update.message.text:
            return
        user_id   = update.effective_user.id
        chat_id   = update.message.chat_id
        user_text = update.message.text.strip()

        if user_text == "/start":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text=(
                "안녕하세요! 자동거래 봇입니다 🚀\n\n"
                "📈 거래 명령어:\n"
                "/starttrading - 자동거래 시작\n"
                "/stoptrading  - 자동거래 중지\n"
                "/balance      - 전체 지갑 잔액 확인\n"
                "/report       - 지금까지 거래 로그 확인\n"
                "/settings     - 현재 설정값 확인\n"
                "/reset        - 대화 초기화"
            ))
            return

        if user_text == "/reset":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text="대화 기록이 초기화되었습니다.")
            return

        if user_text == "/settings":
            await bot.send_message(chat_id=chat_id, text=(
                f"⚙️ 현재 설정값\n\n"
                f"TOKEN_MINT: {TOKEN_MINT}\n"
                f"TOKEN_DECIMALS: {TOKEN_DECIMALS}\n"
                f"MIN_SOL: {MIN_SOL}\n"
                f"MAX_SOL: {MAX_SOL}\n"
                f"슬리피지/수수료: Jupiter Ultra API 자동 관리\n"
                f"추가 지갑 수: {len(EXTRA_WALLET_KEYS)}개\n"
                f"거래 제외 지갑: {EXCLUDE_WALLET_INDEXES}"
            ))
            return

        if user_text == "/report":
            if not daily_log:
                await bot.send_message(chat_id=chat_id, text="아직 거래 내역이 없습니다.")
            else:
                msg = f"📊 현재까지 거래 ({len(daily_log)}건)\n\n" + "\n".join(daily_log[-50:])
                await bot.send_message(chat_id=chat_id, text=msg[:4000])
            return

        if user_text == "/starttrading":
            if is_trading():
                await bot.send_message(chat_id=chat_id, text="이미 자동거래가 실행 중입니다!")
                return

            set_trading(True)
            alert_chat_id   = chat_id
            trading_threads = []

            t_main = threading.Thread(target=main_wallet_loop, args=(BOT_KEYPAIR,), daemon=True)
            t_main.start()
            trading_threads.append(t_main)

            for idx, key in EXTRA_WALLET_KEYS:
                if idx in EXCLUDE_WALLET_INDEXES:
                    logging.info(f"지갑{idx} 거래 제외")
                    continue
                kp    = parse_keypair(key)
                mult  = LARGE_WALLET_MULTIPLIER if idx == LARGE_WALLET_INDEX else 1.0
                label = f"지갑{idx}" + (" (대형)" if mult > 1.0 else "")

                if idx == 1:
                    # 지갑1: 2시간 근처 랜덤 간격 (100~140분)
                    t = threading.Thread(
                        target=extra_wallet_loop,
                        args=(kp, label, mult, WALLET1_WAIT_MIN_SEC, WALLET1_WAIT_MAX_SEC),
                        daemon=True
                    )
                else:
                    t = threading.Thread(target=extra_wallet_loop, args=(kp, label, mult), daemon=True)

                t.start()
                trading_threads.append(t)

            rt = threading.Thread(target=daily_report_loop, args=(chat_id,), daemon=True)
            rt.start()

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ 자동거래 시작!\n\n"
                    f"📌 토큰: {TOKEN_MINT[:8]}...\n"
                    f"📌 거래량: {MIN_SOL}~{MAX_SOL} SOL\n"
                    f"📌 슬리피지/우선순위 수수료: Jupiter Ultra API 자동 관리\n"
                    f"📌 메인: 매수2:매도1, 45~75분 간격\n"
                    f"📌 지갑1: 매수1:매도1, 100~140분(약 2시간) 간격\n"
                    f"📌 그 외 추가 지갑: 매수1:매도1, 4~6시간 간격\n"
                    f"📌 연속 {MAX_CONSECUTIVE_FAILS}회 실패 시 자동 중지\n\n"
                    f"📊 리포트: 매일 01/09/13/17/21시"
                )
            )
            return

        if user_text == "/stoptrading":
            set_trading(False)
            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 중지되었습니다.")
            return

        if user_text == "/balance":
            try:
                msg = f"💰 지갑 잔액\n\n메인 ({BOT_ADDRESS[:8]}...)\n"
                sol = get_sol_balance(BOT_ADDRESS)
                tok = get_token_balance(BOT_ADDRESS, TOKEN_MINT)
                msg += f"SOL: {sol:.4f}\nTOKEN: {tok / (10**TOKEN_DECIMALS):.2f}\n"

                for idx, key in EXTRA_WALLET_KEYS:
                    kp   = parse_keypair(key)
                    addr = str(kp.pubkey())
                    s    = get_sol_balance(addr)
                    t    = get_token_balance(addr, TOKEN_MINT)
                    tag  = " (대형)" if idx == LARGE_WALLET_INDEX else ""
                    msg += f"\n지갑{idx}{tag} ({addr[:8]}...)\nSOL: {s:.4f}\nTOKEN: {t/(10**TOKEN_DECIMALS):.2f}\n"

                await bot.send_message(chat_id=chat_id, text=msg)
            except Exception as e:
                await bot.send_message(chat_id=chat_id, text=f"잔액 조회 오류: {str(e)}")
            return

        # ── AI 대화 ───────────────────────────────────────────────
        if user_id not in conversation_history:
            conversation_history[user_id] = []

        search_context = ""
        if needs_search(user_text):
            try:
                result = tavily_client.search(query=user_text, max_results=3)
                search_context = "\n\n[검색 결과]\n"
                for r in result["results"]:
                    search_context += f"- {r['title']}: {r['content'][:200]}\n"
            except Exception as e:
                logging.error(f"검색 오류: {e}")

        augmented = user_text + search_context if search_context else user_text
        conversation_history[user_id].append({"role": "user", "content": augmented})
        if len(conversation_history[user_id]) > 20:
            conversation_history[user_id] = conversation_history[user_id][-20:]

        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant. Respond in the same language the user uses."}
                ] + conversation_history[user_id],
                max_tokens=1024,
            )
            reply = response.choices[0].message.content
            conversation_history[user_id].append({"role": "assistant", "content": reply})
        except Exception as e:
            reply = f"오류가 발생했습니다: {str(e)}"

        await bot.send_message(chat_id=chat_id, text=reply)

# ─── Flask webhook ────────────────────────────────────────────────
def _run_in_thread(data):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(handle_update(data))
    except Exception as e:
        logging.error(f"handle_update 오류: {e}", exc_info=True)
    finally:
        loop.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logging.info(f"webhook 수신: {str(data)[:200]}")
    t = threading.Thread(target=_run_in_thread, args=(data,), daemon=True)
    t.start()
    return "OK"

@app.route("/set_webhook")
def set_webhook():
    target_url = f"{WEBHOOK_URL}/webhook"
    logging.info(f"웹훅 등록 시도: {target_url}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _set():
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            result = await bot.set_webhook(
                target_url,
                allowed_updates=["message", "callback_query"],
            )
            info = await bot.get_webhook_info()
            return result, info

    try:
        result, info = loop.run_until_complete(_set())
        loop.close()
        last_err = info.last_error_message or "없음"
        return (
            f"✅ Webhook 등록 완료: {result}<br><br>"
            f"등록 URL: {info.url}<br>"
            f"대기 업데이트: {info.pending_update_count}<br>"
            f"마지막 오류: {last_err}"
        )
    except Exception as e:
        loop.close()
        logging.error(f"웹훅 등록 실패: {e}", exc_info=True)
        return f"❌ 웹훅 등록 실패: {str(e)}", 500

@app.route("/webhook_info")
def webhook_info():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _info():
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            return await bot.get_webhook_info()

    try:
        info = loop.run_until_complete(_info())
        loop.close()
        return (
            f"📡 Webhook 상태<br><br>"
            f"URL: {info.url}<br>"
            f"대기 업데이트: {info.pending_update_count}<br>"
            f"마지막 오류: {info.last_error_message or '없음'}<br>"
            f"마지막 오류 시각: {info.last_error_date or '없음'}"
        )
    except Exception as e:
        loop.close()
        return f"❌ 조회 실패: {str(e)}", 500

@app.route("/")
def index():
    status = "🟢 거래 중" if is_trading() else "🔴 거래 중지"
    return (
        f"Bot is running! 🚀<br>"
        f"거래 상태: {status}<br><br>"
        f"<a href='/set_webhook'>웹훅 등록</a> | "
        f"<a href='/webhook_info'>웹훅 상태 확인</a>"
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
