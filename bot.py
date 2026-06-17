import os
import logging
import asyncio
import random
import json
from datetime import datetime
from flask import Flask, request
from telegram import Update, Bot
from groq import Groq
from tavily import TavilyClient
from web3 import Web3

logging.basicConfig(level=logging.INFO)

# ─── 환경변수 ─────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
WEBHOOK_URL     = os.environ["WEBHOOK_URL"]
TAVILY_API_KEY  = os.environ["TAVILY_API_KEY"]
RPC_URL         = os.environ["RPC_URL"]          # Polygon RPC
BOT_PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY"]  # 봇 지갑 프라이빗 키
TOKEN_ADDRESS   = os.environ["TOKEN_ADDRESS"]    # ELAHZ 컨트랙트 주소

# ─── Web3 초기화 ──────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(RPC_URL))
BOT_WALLET  = w3.eth.account.from_key(BOT_PRIVATE_KEY)
BOT_ADDRESS = BOT_WALLET.address

# ─── Uniswap V3 SwapRouter02 주소 (Polygon) ───────────────────────
ROUTER_ADDRESS = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

# WMATIC 주소 (Polygon)
WMATIC_ADDRESS = "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"

# ─── Uniswap V3 SwapRouter ABI (필요한 함수만) ────────────────────
ROUTER_ABI = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IV3SwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"},{"inputs":[{"internalType":"bytes[]","name":"data","type":"bytes[]"}],"name":"multicall","outputs":[{"internalType":"bytes[]","name":"results","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]')

# ─── ERC20 ABI ────────────────────────────────────────────────────
ERC20_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"}],"name":"allowance","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')

# ─── 거래 설정 ────────────────────────────────────────────────────
MIN_POL = 0.5    # 최소 거래량 (POL)
MAX_POL = 2.0    # 최대 거래량 (POL)
FEE     = 10000  # Uniswap V3 수수료 티어 (1% = 10000)

# ─── 기타 ─────────────────────────────────────────────────────────
groq_client   = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
conversation_history = {}
trading_active = False
app = Flask(__name__)

SEARCH_KEYWORDS = ["현재", "지금", "오늘", "최신", "최근", "주가", "날씨", "뉴스", "환율", "가격", "몇시", "누구야", "대통령", "총리", "결과"]

def needs_search(text):
    return any(keyword in text for keyword in SEARCH_KEYWORDS)

def get_random_pol_amount():
    amount   = random.uniform(MIN_POL, MAX_POL)
    decimals = random.randint(4, 7)
    return round(amount, decimals)

def get_nonce():
    return w3.eth.get_transaction_count(BOT_ADDRESS, 'pending')

def get_gas_price():
    # Polygon은 가스비가 낮지만 너무 낮으면 실패, 약간 높게 설정
    base = w3.eth.gas_price
    return int(base * 1.2)

# ─── 매수 함수 (POL → ELAHZ) ─────────────────────────────────────
def buy_elahz(pol_amount):
    try:
        router   = w3.eth.contract(address=w3.to_checksum_address(ROUTER_ADDRESS), abi=ROUTER_ABI)
        amount_in = w3.to_wei(pol_amount, 'ether')
        deadline  = int(datetime.now().timestamp()) + 300
        nonce     = get_nonce()
        gas_price = get_gas_price()

        params = {
            "tokenIn":           w3.to_checksum_address(WMATIC_ADDRESS),
            "tokenOut":          w3.to_checksum_address(TOKEN_ADDRESS),
            "fee":               FEE,
            "recipient":         BOT_ADDRESS,
            "amountIn":          amount_in,
            "amountOutMinimum":  0,
            "sqrtPriceLimitX96": 0,
        }

        tx = router.functions.exactInputSingle(params).build_transaction({
            'from':     BOT_ADDRESS,
            'value':    amount_in,
            'gas':      300000,
            'gasPrice': gas_price,
            'nonce':    nonce,
        })

        signed   = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        return tx_hash.hex(), receipt.status

    except Exception as e:
        logging.error(f"매수 오류: {e}")
        return None, 0

# ─── 매도 함수 (ELAHZ → POL) ─────────────────────────────────────
def sell_elahz():
    try:
        router    = w3.eth.contract(address=w3.to_checksum_address(ROUTER_ADDRESS), abi=ROUTER_ABI)
        token     = w3.eth.contract(address=w3.to_checksum_address(TOKEN_ADDRESS), abi=ERC20_ABI)
        gas_price = get_gas_price()

        # 잔액의 40~70% 매도
        balance   = token.functions.balanceOf(BOT_ADDRESS).call()
        amount_in = int(balance * random.uniform(0.4, 0.7))

        if amount_in == 0:
            logging.warning("ELAHZ 잔액 없음, 매도 스킵")
            return None, 0

        nonce    = get_nonce()
        deadline = int(datetime.now().timestamp()) + 300

        # ── Approve ──────────────────────────────────────────────
        allowance = token.functions.allowance(BOT_ADDRESS, w3.to_checksum_address(ROUTER_ADDRESS)).call()
        if allowance < amount_in:
            approve_tx = token.functions.approve(
                w3.to_checksum_address(ROUTER_ADDRESS),
                2**256 - 1
            ).build_transaction({
                'from':     BOT_ADDRESS,
                'gas':      100000,
                'gasPrice': gas_price,
                'nonce':    nonce,
            })
            signed_approve  = w3.eth.account.sign_transaction(approve_tx, BOT_PRIVATE_KEY)
            approve_hash    = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
            w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)  # ← 수정: approve_hash 사용
            nonce += 1
            logging.info("Approve 완료")

        # ── Swap ─────────────────────────────────────────────────
        params = {
            "tokenIn":           w3.to_checksum_address(TOKEN_ADDRESS),
            "tokenOut":          w3.to_checksum_address(WMATIC_ADDRESS),
            "fee":               FEE,
            "recipient":         BOT_ADDRESS,
            "amountIn":          amount_in,
            "amountOutMinimum":  0,
            "sqrtPriceLimitX96": 0,
        }

        tx = router.functions.exactInputSingle(params).build_transaction({
            'from':     BOT_ADDRESS,
            'value':    0,
            'gas':      300000,
            'gasPrice': gas_price,
            'nonce':    nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        return tx_hash.hex(), receipt.status

    except Exception as e:
        logging.error(f"매도 오류: {e}")
        return None, 0

# ─── 자동거래 루프 ────────────────────────────────────────────────
async def auto_trade(bot, chat_id):
    global trading_active
    await bot.send_message(chat_id=chat_id, text="🤖 자동거래 루프 시작!")

    while trading_active:
        try:
            pol_amount = get_random_pol_amount()

            # ── 매수 ─────────────────────────────────────────────
            await bot.send_message(chat_id=chat_id, text=f"🔄 매수 시도 중... {pol_amount} POL")
            await asyncio.sleep(random.randint(1, 5))

            tx_hash, status = await asyncio.get_event_loop().run_in_executor(None, buy_elahz, pol_amount)

            if status == 1:
                await bot.send_message(chat_id=chat_id, text=f"✅ 매수 완료\nPOL: {pol_amount}\nhttps://polygonscan.com/tx/{tx_hash}")
            else:
                await bot.send_message(chat_id=chat_id, text=f"❌ 매수 실패 (TX: {tx_hash})")

            # ── 매수 후 대기 (25~35분, 총 1시간 사이클 맞추기) ──────
            wait = random.randint(1500, 2100)
            await bot.send_message(chat_id=chat_id, text=f"⏳ {wait//60}분 후 매도 예정")
            await asyncio.sleep(wait)

            if not trading_active:
                break

            # ── 매도 ─────────────────────────────────────────────
            await bot.send_message(chat_id=chat_id, text="🔄 매도 시도 중...")
            await asyncio.sleep(random.randint(1, 5))

            tx_hash, status = await asyncio.get_event_loop().run_in_executor(None, sell_elahz)

            if status == 1:
                await bot.send_message(chat_id=chat_id, text=f"✅ 매도 완료\nhttps://polygonscan.com/tx/{tx_hash}")
            else:
                await bot.send_message(chat_id=chat_id, text=f"❌ 매도 실패 (TX: {tx_hash})")

            # ── 다음 거래까지 대기 (25~35분) ─────────────────────
            wait = random.randint(1500, 2100)
            await bot.send_message(chat_id=chat_id, text=f"⏳ {wait//60}분 후 다음 거래 예정")
            await asyncio.sleep(wait)

        except Exception as e:
            logging.error(f"자동거래 오류: {e}")
            await bot.send_message(chat_id=chat_id, text=f"⚠️ 오류 발생: {str(e)}\n60초 후 재시도")
            await asyncio.sleep(60)

    await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 루프 종료")

# ─── 텔레그램 핸들러 ──────────────────────────────────────────────
async def handle_update(update_data):
    global trading_active
    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        update    = Update.de_json(update_data, bot)
        if not update.message or not update.message.text:
            return
        user_id   = update.effective_user.id
        chat_id   = update.message.chat_id
        user_text = update.message.text

        # ── 명령어 처리 ──────────────────────────────────────────
        if user_text == "/start":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text=(
                "안녕하세요! 무엇이든 물어보세요.\n\n"
                "📈 거래 명령어:\n"
                "/starttrading - 자동거래 시작\n"
                "/stoptrading  - 자동거래 중지\n"
                "/balance      - 잔액 확인\n"
                "/reset        - 대화 초기화"
            ))
            return

        if user_text == "/reset":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text="대화 기록이 초기화되었습니다.")
            return

        if user_text == "/starttrading":
            if trading_active:
                await bot.send_message(chat_id=chat_id, text="이미 자동거래가 실행 중입니다!")
                return
            trading_active = True
            asyncio.create_task(auto_trade(bot, chat_id))
            return

        if user_text == "/stoptrading":
            trading_active = False
            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 중지되었습니다.")
            return

        if user_text == "/balance":
            try:
                pol_balance   = w3.eth.get_balance(BOT_ADDRESS)
                pol           = w3.from_wei(pol_balance, 'ether')
                token         = w3.eth.contract(address=w3.to_checksum_address(TOKEN_ADDRESS), abi=ERC20_ABI)
                elahz_balance = token.functions.balanceOf(BOT_ADDRESS).call()
                elahz         = elahz_balance / 10**18
                await bot.send_message(chat_id=chat_id, text=f"💰 봇 지갑 잔액\nPOL: {pol:.6f}\nELAHZ: {elahz:.4f}")
            except Exception as e:
                await bot.send_message(chat_id=chat_id, text=f"잔액 조회 오류: {str(e)}")
            return

        # ── AI 대화 ───────────────────────────────────────────────
        if user_id not in conversation_history:
            conversation_history[user_id] = []

        search_context = ""
        if needs_search(user_text):
            try:
                result         = tavily_client.search(query=user_text, max_results=3)
                search_context = "\n\n[검색 결과]\n"
                for r in result["results"]:
                    search_context += f"- {r['title']}: {r['content'][:200]}\n"
            except Exception as e:
                logging.error(f"검색 오류: {e}")

        augmented_text = user_text + search_context if search_context else user_text
        conversation_history[user_id].append({"role": "user", "content": augmented_text})

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

# ─── Flask 라우트 ─────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    asyncio.run(handle_update(update_data))
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
    return "Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
