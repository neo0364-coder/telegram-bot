import os
import logging
import asyncio
import random
import json
import threading
import time
import base64
from datetime import datetime
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

# ─── 환경변수 ─────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
WEBHOOK_URL     = os.environ["WEBHOOK_URL"]
TAVILY_API_KEY  = os.environ["TAVILY_API_KEY"]

RPC_URL         = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
BOT_PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY2"]
TOKEN_MINT      = os.environ.get("TOKEN_MINT", "GNEuYzCanJP7rj4BB1VGh53JWhWkbeKVYDpzNzsg4hyh")

EXTRA_WALLET_KEYS = []
for i in range(1, 11):
    key = os.environ.get(f"WALLET_{i}_KEY")
    if key:
        EXTRA_WALLET_KEYS.append((i, key))

LARGE_WALLET_INDEX      = 5
LARGE_WALLET_MULTIPLIER = 1.5

# ─── Jupiter API (메인 + 폴백) ────────────────────────────────────
WSOL_MINT = "So11111111111111111111111111111111111111112"

JUPITER_QUOTE_ENDPOINTS = [
    "https://lite-api.jup.ag/swap/v1/quote",
    "https://quote-api.jup.ag/v6/quote",
]
JUPITER_SWAP_ENDPOINTS = [
    "https://lite-api.jup.ag/swap/v1/swap",
    "https://quote-api.jup.ag/v6/swap",
]

# ─── 안전 설정 ────────────────────────────────────────────────────
MIN_SOL            = 0.003   # 소량
MAX_SOL            = 0.008   # 소량
MAX_SELL_PCT       = 5
SLIPPAGE_BPS       = 50      # 0.5%
PRIORITY_FEE_MICRO = 300000

# ─── 스레드 안전 상태 관리 ────────────────────────────────────────
_state_lock          = threading.Lock()
trading_active       = False
trading_threads      = []
daily_log            = []
conversation_history = {}

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

# ─── Jupiter Quote (재시도 + 폴백) ───────────────────────────────
def jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int) -> dict:
    last_exc = None
    for url in JUPITER_QUOTE_ENDPOINTS:
        for attempt in range(3):
            try:
                resp = httpx.get(url, params={
                    "inputMint":           input_mint,
                    "outputMint":          output_mint,
                    "amount":              amount_lamports,
                    "slippageBps":         SLIPPAGE_BPS,
                    "onlyDirectRoutes":    "false",
                    "asLegacyTransaction": "false",
                }, timeout=15)
                logging.info(f"Jupiter quote [{url}] 응답: {resp.status_code} {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exc = e
                logging.warning(f"Jupiter quote 실패 [{url}] ({attempt+1}/3): {e}")
                time.sleep(5 * (attempt + 1))
            except httpx.HTTPStatusError as e:
                last_exc = e
                logging.warning(f"Jupiter quote HTTP 오류 [{url}]: {e}")
                break
    raise RuntimeError(f"Jupiter quote 모든 엔드포인트 실패: {last_exc}")

# ─── Jupiter Swap (재시도 + 폴백) ────────────────────────────────
def jupiter_swap_tx(quote: dict, user_pubkey: str) -> str:
    last_exc = None
    for url in JUPITER_SWAP_ENDPOINTS:
        for attempt in range(3):
            try:
                resp = httpx.post(url, json={
                    "quoteResponse":             quote,
                    "userPublicKey":             user_pubkey,
                    "wrapAndUnwrapSol":          True,
                    "prioritizationFeeLamports": PRIORITY_FEE_MICRO,
                    "dynamicComputeUnitLimit":   True,
                }, timeout=15)
                logging.info(f"Jupiter swap [{url}] 응답: {resp.status_code} {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()["swapTransaction"]
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exc = e
                logging.warning(f"Jupiter swap 실패 [{url}] ({attempt+1}/3): {e}")
                time.sleep(5 * (attempt + 1))
            except httpx.HTTPStatusError as e:
                last_exc = e
                logging.warning(f"Jupiter swap HTTP 오류 [{url}]: {e}")
                break
    raise RuntimeError(f"Jupiter swap 모든 엔드포인트 실패: {last_exc}")

# ─── 서명 및 전송 ─────────────────────────────────────────────────
def sign_and_send(tx_b64: str, keypair: Keypair) -> tuple[str, bool]:
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
    tx_sig     = send_transaction(signed_b64)
    ok         = confirm_transaction(tx_sig)
    return tx_sig, ok

# ─── 매수 (SOL → TOKEN) ───────────────────────────────────────────
def buy_elaz(keypair: Keypair, multiplier: float = 1.0):
    try:
        pubkey     = str(keypair.pubkey())
        sol_amount = random.uniform(MIN_SOL, MAX_SOL) * multiplier
        lamports   = int(sol_amount * 1e9)

        sol_bal = get_sol_balance(pubkey)
        if sol_bal < sol_amount + 0.005:
            logging.warning(f"[{pubkey[:8]}] SOL 잔액 부족: {sol_bal:.4f}")
            return None, False, f"SOL 잔액 부족 ({sol_bal:.4f})"

        logging.info(f"[{pubkey[:8]}] 매수 시도: {sol_amount:.4f} SOL")
        quote   = jupiter_quote(WSOL_MINT, TOKEN_MINT, lamports)
        tx_b64  = jupiter_swap_tx(quote, pubkey)
        sig, ok = sign_and_send(tx_b64, keypair)
        logging.info(f"[{pubkey[:8]}] 매수 결과: {'성공' if ok else '실패'} sig={sig}")
        return sig, ok, f"{sol_amount:.4f} SOL"

    except Exception as e:
        logging.error(f"매수 오류 ({str(keypair.pubkey())[:8]}): {e}", exc_info=True)
        return None, False, str(e)[:100]

# ─── 매도 (TOKEN → SOL) ───────────────────────────────────────────
def sell_elaz(keypair: Keypair, multiplier: float = 1.0):
    try:
        pubkey  = str(keypair.pubkey())
        balance = get_token_balance(pubkey, TOKEN_MINT)
        if balance == 0:
            logging.warning(f"[{pubkey[:8]}] ELAZ 잔액 없음")
            return None, False, "ELAZ 잔액없음"

        pct       = random.uniform(1, MAX_SELL_PCT) / 100 * multiplier
        pct       = min(pct, 0.95)
        amount_in = int(balance * pct)
        if amount_in == 0:
            return None, False, "매도량 0"

        logging.info(f"[{pubkey[:8]}] 매도 시도: {amount_in} ELAZ raw")
        quote   = jupiter_quote(TOKEN_MINT, WSOL_MINT, amount_in)
        tx_b64  = jupiter_swap_tx(quote, pubkey)
        sig, ok = sign_and_send(tx_b64, keypair)

        readable = amount_in / (10 ** 6)
        logging.info(f"[{pubkey[:8]}] 매도 결과: {'성공' if ok else '실패'} sig={sig}")
        return sig, ok, f"{readable:.2f} ELAZ"

    except Exception as e:
        logging.error(f"매도 오류 ({str(keypair.pubkey())[:8]}): {e}", exc_info=True)
        return None, False, str(e)[:100]

# ─── 로그 기록 헬퍼 ──────────────────────────────────────────────
def log_trade(label: str, action: str, sig, ok: bool, info: str):
    ts  = datetime.now().strftime("%H:%M")
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
# /starttrading 즉시 매수 → 매수 2회마다 매도 1회 → 45~75분 간격
def main_wallet_loop(keypair: Keypair):
    label     = "메인"
    buy_count = 0  # 연속 매수 횟수 추적
    logging.info(f"[{label}] 거래 루프 시작 — 즉시 첫 매수 (매수2:매도1 패턴)")

    while is_trading():
        try:
            if buy_count < 2:
                # 매수
                sig, ok, info = buy_elaz(keypair)
                log_trade(label, "매수", sig, ok, info)
                if ok:
                    buy_count += 1
                    logging.info(f"[{label}] 매수 누적 {buy_count}/2")
            else:
                # 매수 2회 완료 → 매도 1회
                sig, ok, info = sell_elaz(keypair)
                log_trade(label, "매도", sig, ok, info)
                if ok:
                    buy_count = 0
                    logging.info(f"[{label}] 매도 완료 — 카운트 초기화")

            # 45~75분 랜덤 간격
            wait = random.randint(45 * 60, 75 * 60)
            logging.info(f"[{label}] 다음 거래까지 {wait//60}분 대기")
            if not interruptible_sleep(wait):
                return

        except Exception as e:
            logging.error(f"[{label}] 루프 오류: {e}", exc_info=True)
            time.sleep(60)

# ─── 추가 지갑 루프 ───────────────────────────────────────────────
# 하루 4~5회, 매수/매도 번갈아, 초기 랜덤 딜레이로 지갑 분산
def extra_wallet_loop(keypair: Keypair, wallet_label: str, multiplier: float = 1.0):
    # 초기 딜레이: 0~6시간 랜덤
    initial_delay = random.randint(0, 6 * 3600)
    logging.info(f"[{wallet_label}] 거래 루프 시작 — 초기 대기 {initial_delay//60}분")
    if not interruptible_sleep(initial_delay):
        return

    next_action = "buy"

    while is_trading():
        try:
            if next_action == "buy":
                sig, ok, info = buy_elaz(keypair, multiplier)
                log_trade(wallet_label, "매수", sig, ok, info)
                next_action = "sell"
            else:
                sig, ok, info = sell_elaz(keypair, multiplier)
                log_trade(wallet_label, "매도", sig, ok, info)
                next_action = "buy"

            # 하루 4~5회 → 거래 간격 240~360분 랜덤 (기존과 동일)
            wait = random.randint(240 * 60, 360 * 60)
            logging.info(f"[{wallet_label}] 다음 거래까지 {wait//60}분 대기")
            if not interruptible_sleep(wait):
                return

        except Exception as e:
            logging.error(f"[{wallet_label}] 루프 오류: {e}", exc_info=True)
            time.sleep(60)

# ─── 하루 2회 보고 스케줄러 ───────────────────────────────────────
def daily_report_loop(chat_id):
    async def send_report():
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            if not daily_log:
                msg = "📊 일일 리포트\n오늘 거래 내역이 없습니다."
            else:
                msg = f"📊 일일 리포트 ({len(daily_log)}건)\n\n" + "\n".join(daily_log[-50:])
            await bot.send_message(chat_id=chat_id, text=msg[:4000])
            daily_log.clear()

    while is_trading():
        now          = datetime.now()
        target_hours = [9, 21]
        next_hour    = next((h for h in target_hours if h > now.hour), None)
        if next_hour:
            wait = (next_hour - now.hour) * 3600 - now.minute * 60
        else:
            wait = (24 - now.hour + target_hours[0]) * 3600 - now.minute * 60
        wait = max(wait, 60)

        if not interruptible_sleep(min(wait, 3600)):
            return

        now2 = datetime.now()
        if now2.hour in target_hours and now2.minute < 5:
            asyncio.run(send_report())
            time.sleep(300)

# ─── 텔레그램 핸들러 ──────────────────────────────────────────────
async def handle_update(update_data):
    global trading_threads
    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        update = Update.de_json(update_data, bot)
        if not update.message or not update.message.text:
            return
        user_id   = update.effective_user.id
        chat_id   = update.message.chat_id
        user_text = update.message.text.strip()

        # ── /start ──
        if user_text == "/start":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text=(
                "안녕하세요! ELAZ 자동거래 봇입니다 🚀\n\n"
                "📈 거래 명령어:\n"
                "/starttrading - 자동거래 시작\n"
                "/stoptrading  - 자동거래 중지\n"
                "/balance      - 전체 지갑 잔액 확인\n"
                "/report       - 지금까지 거래 로그 확인\n"
                "/reset        - 대화 초기화"
            ))
            return

        # ── /reset ──
        if user_text == "/reset":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text="대화 기록이 초기화되었습니다.")
            return

        # ── /report ──
        if user_text == "/report":
            if not daily_log:
                await bot.send_message(chat_id=chat_id, text="아직 거래 내역이 없습니다.")
            else:
                msg = f"📊 현재까지 거래 ({len(daily_log)}건)\n\n" + "\n".join(daily_log[-50:])
                await bot.send_message(chat_id=chat_id, text=msg[:4000])
            return

        # ── /starttrading ──
        if user_text == "/starttrading":
            if is_trading():
                await bot.send_message(chat_id=chat_id, text="이미 자동거래가 실행 중입니다!")
                return

            set_trading(True)
            trading_threads = []

            # 메인 지갑: 즉시 매수, 45~75분 간격으로 매수/매도 번갈아
            t_main = threading.Thread(
                target=main_wallet_loop,
                args=(BOT_KEYPAIR,),
                daemon=True
            )
            t_main.start()
            trading_threads.append(t_main)

            # 추가 지갑: 랜덤 딜레이 후 하루 4~5회 매수/매도 번갈아
            for idx, key in EXTRA_WALLET_KEYS:
                kp    = parse_keypair(key)
                mult  = LARGE_WALLET_MULTIPLIER if idx == LARGE_WALLET_INDEX else 1.0
                label = f"지갑{idx}" + (" (대형)" if mult > 1.0 else "")
                t = threading.Thread(
                    target=extra_wallet_loop,
                    args=(kp, label, mult),
                    daemon=True
                )
                t.start()
                trading_threads.append(t)

            # 보고 스케줄러
            rt = threading.Thread(target=daily_report_loop, args=(chat_id,), daemon=True)
            rt.start()

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ ELAZ 자동거래 시작!\n\n"
                    f"📌 거래량: {MIN_SOL}~{MAX_SOL} SOL (소량)\n"
                    f"📌 슬리피지: {SLIPPAGE_BPS/100:.1f}%\n"
                    f"📌 패턴: 매수/매도 1:1 번갈아\n\n"
                    f"🔹 메인 지갑: 즉시 시작, 45~75분 간격\n"
                    f"🔹 추가 지갑 {len(EXTRA_WALLET_KEYS)}개: 랜덤 분산, 하루 4~5회\n"
                    f"   (지갑{LARGE_WALLET_INDEX}은 거래량 {LARGE_WALLET_MULTIPLIER}배)\n\n"
                    f"📊 리포트: 매일 오전9시/오후9시"
                )
            )
            return

        # ── /stoptrading ──
        if user_text == "/stoptrading":
            set_trading(False)
            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 중지되었습니다.")
            return

        # ── /balance ──
        if user_text == "/balance":
            try:
                msg = f"💰 지갑 잔액\n\n메인 ({BOT_ADDRESS[:8]}...)\n"
                sol = get_sol_balance(BOT_ADDRESS)
                tok = get_token_balance(BOT_ADDRESS, TOKEN_MINT)
                msg += f"SOL: {sol:.4f}\nELAZ: {tok / 1e6:.2f}\n"

                for idx, key in EXTRA_WALLET_KEYS:
                    kp   = parse_keypair(key)
                    addr = str(kp.pubkey())
                    s    = get_sol_balance(addr)
                    t    = get_token_balance(addr, TOKEN_MINT)
                    tag  = " (대형)" if idx == LARGE_WALLET_INDEX else ""
                    msg += f"\n지갑{idx}{tag} ({addr[:8]}...)\nSOL: {s:.4f}\nELAZ: {t/1e6:.2f}\n"

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
    finally:
        loop.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    t = threading.Thread(target=_run_in_thread, args=(data,), daemon=True)
    t.start()
    return "OK"

@app.route("/set_webhook")
def set_webhook():
    async def _set():
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    asyncio.run(_set())
    return f"Webhook set to {WEBHOOK_URL}/webhook"

@app.route("/")
def index():
    return "ELAZ Bot is running! 🚀"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
