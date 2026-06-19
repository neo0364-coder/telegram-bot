import os
import logging
import asyncio
import random
import json
import threading
import time
import base64
import struct
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

# Solana RPC (Helius 또는 다른 RPC URL)
RPC_URL         = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")

BOT_PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY2"]   # base58 또는 JSON 배열

# ELAZ 토큰 Mint 주소
TOKEN_MINT      = os.environ.get("TOKEN_MINT", "GNEuYzCanJP7rj4BB1VGh53JWhWkbeKVYDpzNzsg4hyh")

# 추가 지갑 (WALLET_1_KEY ~ WALLET_10_KEY)
EXTRA_WALLET_KEYS = []
for i in range(1, 11):
    key = os.environ.get(f"WALLET_{i}_KEY")
    if key:
        EXTRA_WALLET_KEYS.append((i, key))

LARGE_WALLET_INDEX      = 5
LARGE_WALLET_MULTIPLIER = 3.0

# ─── Raydium / Jupiter 관련 주소 ──────────────────────────────────
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Jupiter Aggregator API (Raydium CPMM 풀 포함 자동 라우팅)
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API  = "https://quote-api.jup.ag/v6/swap"

# ─── 안전 설정 ────────────────────────────────────────────────────
MIN_SOL             = 0.01    # 최소 매수 SOL (기존 MIN_POL 0.3 → SOL 단가 고려해 0.01)
MAX_SOL             = 0.05    # 최대 매수 SOL (기존 MAX_POL 1.0 → 0.05)
MAX_SELL_PCT        = 8       # 보유량의 최대 8% 매도
SLIPPAGE_BPS        = 300     # 3% 슬리피지 (300 bps)
PRIORITY_FEE_MICRO  = 200000  # 우선순위 수수료 (micro-lamports)

# ─── 키 파싱 유틸 ─────────────────────────────────────────────────
def parse_keypair(raw: str) -> Keypair:
    raw = raw.strip().strip('"').strip("'")
    if raw.startswith("0x") or raw.startswith("0X"):
        raw = raw[2:]
    if raw.startswith("["):
        arr = json.loads(raw)
        return Keypair.from_bytes(bytes(arr))
    else:
        from solders.keypair import Keypair as KP
        return KP.from_base58_string(raw)

# ─── 봇/지갑 초기화 ───────────────────────────────────────────────
BOT_KEYPAIR = parse_keypair(BOT_PRIVATE_KEY)
BOT_ADDRESS = str(BOT_KEYPAIR.pubkey())

groq_client          = Groq(api_key=GROQ_API_KEY)
tavily_client        = TavilyClient(api_key=TAVILY_API_KEY)
conversation_history = {}
trading_active       = False
trading_threads      = []
daily_log            = []
app                  = Flask(__name__)

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
    """SOL 잔액 조회 (SOL 단위)"""
    result = rpc_post("getBalance", [pubkey, {"commitment": "confirmed"}])
    return result["value"] / 1e9

def get_token_balance(pubkey: str, mint: str) -> int:
    """SPL 토큰 잔액 조회 (raw amount, lamports 단위)"""
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

def get_token_decimals(mint: str) -> int:
    result = rpc_post("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    return result["value"]["data"]["parsed"]["info"]["decimals"]

def get_latest_blockhash() -> str:
    result = rpc_post("getLatestBlockhash", [{"commitment": "finalized"}])
    return result["value"]["blockhash"]

def send_transaction(signed_tx_b64: str) -> str:
    result = rpc_post("sendTransaction", [
        signed_tx_b64,
        {"encoding": "base64", "preflightCommitment": "confirmed", "skipPreflight": False}
    ])
    return result  # tx signature

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

# ─── Jupiter 스왑 ─────────────────────────────────────────────────
def jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int) -> dict:
    resp = httpx.get(JUPITER_QUOTE_API, params={
        "inputMint":         input_mint,
        "outputMint":        output_mint,
        "amount":            amount_lamports,
        "slippageBps":       SLIPPAGE_BPS,
        "onlyDirectRoutes":  False,
        "asLegacyTransaction": False,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()

def jupiter_swap_tx(quote: dict, user_pubkey: str) -> str:
    """Jupiter에서 서명 전 트랜잭션 직렬화(base64) 반환"""
    resp = httpx.post(JUPITER_SWAP_API, json={
        "quoteResponse":              quote,
        "userPublicKey":              user_pubkey,
        "wrapAndUnwrapSol":           True,
        "prioritizationFeeLamports":  PRIORITY_FEE_MICRO,
        "dynamicComputeUnitLimit":    True,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["swapTransaction"]

def sign_and_send(tx_b64: str, keypair: Keypair) -> tuple[str, bool]:
    """트랜잭션 서명 후 전송, (signature, success) 반환"""
    raw = base64.b64decode(tx_b64)
    tx  = VersionedTransaction.from_bytes(raw)
    msg_bytes = to_bytes_versioned(tx.message)
    sig = keypair.sign_message(msg_bytes)
    signed = VersionedTransaction.populate(tx.message, [sig])
    signed_b64 = base64.b64encode(bytes(signed)).decode()
    tx_sig = send_transaction(signed_b64)
    ok = confirm_transaction(tx_sig)
    return tx_sig, ok

# ─── 매수 (SOL → ELAZ) ────────────────────────────────────────────
def buy_elaz(keypair: Keypair, multiplier: float = 1.0):
    try:
        pubkey = str(keypair.pubkey())
        sol_amount = random.uniform(MIN_SOL, MAX_SOL) * multiplier
        lamports   = int(sol_amount * 1e9)

        # SOL 잔액 확인 (수수료 포함 여유 체크)
        sol_bal = get_sol_balance(pubkey)
        if sol_bal < sol_amount + 0.005:
            return None, False, f"SOL 잔액 부족 ({sol_bal:.4f})"

        quote  = jupiter_quote(WSOL_MINT, TOKEN_MINT, lamports)
        tx_b64 = jupiter_swap_tx(quote, pubkey)
        sig, ok = sign_and_send(tx_b64, keypair)
        return sig, ok, f"{sol_amount:.4f} SOL"

    except Exception as e:
        logging.error(f"매수 오류 ({str(keypair.pubkey())[:8]}): {e}")
        return None, False, str(e)[:100]

# ─── 매도 (ELAZ → SOL) ────────────────────────────────────────────
def sell_elaz(keypair: Keypair, multiplier: float = 1.0):
    try:
        pubkey  = str(keypair.pubkey())
        balance = get_token_balance(pubkey, TOKEN_MINT)
        if balance == 0:
            return None, False, "ELAZ 잔액없음"

        pct        = random.uniform(2, MAX_SELL_PCT) / 100 * multiplier
        pct        = min(pct, 0.95)
        amount_in  = int(balance * pct)
        if amount_in == 0:
            return None, False, "매도량 0"

        quote  = jupiter_quote(TOKEN_MINT, WSOL_MINT, amount_in)
        tx_b64 = jupiter_swap_tx(quote, pubkey)
        sig, ok = sign_and_send(tx_b64, keypair)

        decimals = 6  # ELAZ 소수점 (필요시 get_token_decimals(TOKEN_MINT) 호출)
        readable = amount_in / (10 ** decimals)
        return sig, ok, f"{readable:.2f} ELAZ"

    except Exception as e:
        logging.error(f"매도 오류 ({str(keypair.pubkey())[:8]}): {e}")
        return None, False, str(e)[:100]

# ─── 지갑별 자동거래 루프 ─────────────────────────────────────────
def wallet_trading_loop(keypair: Keypair, min_wait_sec: int, max_wait_sec: int,
                        wallet_label: str, multiplier: float = 1.0):
    global trading_active
    time.sleep(random.randint(0, 600))  # 지갑마다 랜덤 시작 딜레이

    while trading_active:
        try:
            # ── 매수 ──
            sig, ok, info = buy_elaz(keypair, multiplier)
            ts = datetime.now().strftime("%H:%M")
            tag = "✅" if ok else "❌"
            daily_log.append(f"{tag} [{wallet_label}/Raydium] {ts} 매수 {info}")
            if sig:
                daily_log.append(f"   └ https://solscan.io/tx/{sig}")

            wait = random.randint(min_wait_sec, max_wait_sec)
            time.sleep(wait)

            if not trading_active:
                break

            # ── 매도 ──
            sig, ok, info = sell_elaz(keypair, multiplier)
            ts = datetime.now().strftime("%H:%M")
            tag = "✅" if ok else "❌"
            daily_log.append(f"{tag} [{wallet_label}/Raydium] {ts} 매도 {info}")
            if sig:
                daily_log.append(f"   └ https://solscan.io/tx/{sig}")

            wait = random.randint(min_wait_sec, max_wait_sec)
            time.sleep(wait)

        except Exception as e:
            logging.error(f"{wallet_label} 루프 오류: {e}")
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

    while trading_active:
        now = datetime.now()
        target_hours = [9, 21]
        next_hour = min([h for h in target_hours if h > now.hour], default=None)
        if next_hour:
            wait = (next_hour - now.hour) * 3600 - now.minute * 60
        else:
            wait = (24 - now.hour + target_hours[0]) * 3600 - now.minute * 60
        wait = max(wait, 60)
        time.sleep(min(wait, 3600))
        now2 = datetime.now()
        if now2.hour in target_hours and now2.minute < 5:
            asyncio.run(send_report())
            time.sleep(300)

# ─── 텔레그램 핸들러 ──────────────────────────────────────────────
async def handle_update(update_data):
    global trading_active, trading_threads
    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        update    = Update.de_json(update_data, bot)
        if not update.message or not update.message.text:
            return
        user_id   = update.effective_user.id
        chat_id   = update.message.chat_id
        user_text = update.message.text

        if user_text == "/start":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text=(
                "안녕하세요! ELAZ 자동거래 봇입니다 🚀\n\n"
                "📈 거래 명령어:\n"
                "/starttrading - 자동거래 시작 (전체 지갑)\n"
                "/stoptrading  - 자동거래 중지\n"
                "/balance      - 전체 지갑 잔액 확인\n"
                "/report       - 지금까지 거래 로그 확인\n"
                "/reset        - 대화 초기화"
            ))
            return

        if user_text == "/reset":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text="대화 기록이 초기화되었습니다.")
            return

        if user_text == "/report":
            if not daily_log:
                await bot.send_message(chat_id=chat_id, text="아직 거래 내역이 없습니다.")
            else:
                msg = f"📊 현재까지 거래 ({len(daily_log)}건)\n\n" + "\n".join(daily_log[-50:])
                await bot.send_message(chat_id=chat_id, text=msg[:4000])
            return

        if user_text == "/starttrading":
            if trading_active:
                await bot.send_message(chat_id=chat_id, text="이미 자동거래가 실행 중입니다!")
                return
            trading_active = True
            trading_threads = []

            # 메인 지갑: 하루 약 12회 (25~35분 간격)
            t1 = threading.Thread(
                target=wallet_trading_loop,
                args=(BOT_KEYPAIR, 1500, 2100, "메인"),
                daemon=True
            )
            t1.start()
            trading_threads.append(t1)

            # 추가 지갑: 하루 5~6회 (2~4.5시간 간격)
            for idx, key in EXTRA_WALLET_KEYS:
                kp   = parse_keypair(key)
                mult = LARGE_WALLET_MULTIPLIER if idx == LARGE_WALLET_INDEX else 1.0
                label = f"지갑{idx}" + (" (대형)" if mult > 1.0 else "")
                t = threading.Thread(
                    target=wallet_trading_loop,
                    args=(kp, 7200, 16200, label, mult),
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
                    f"✅ ELAZ 자동거래 시작!\n"
                    f"총 {1 + len(EXTRA_WALLET_KEYS)}개 지갑 운영\n"
                    f"- 메인 지갑: 하루 ~12회\n"
                    f"- 추가 지갑: 각 하루 ~5~6회 (지갑{LARGE_WALLET_INDEX}은 거래금액 {LARGE_WALLET_MULTIPLIER}배)\n"
                    f"거래소: Raydium CPMM (Jupiter 라우팅)\n"
                    f"슬리피지: {SLIPPAGE_BPS/100:.1f}%\n"
                    f"📊 보고: 매일 오전9시/오후9시"
                )
            )
            return

        if user_text == "/stoptrading":
            trading_active = False
            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 중지되었습니다.")
            return

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

# ─── Flask ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    asyncio.run(handle_update(request.get_json(force=True)))
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
