import os
import logging
import asyncio
import random
import json
import threading
from datetime import datetime
from flask import Flask, request
from telegram import Update, Bot
from groq import Groq
from tavily import TavilyClient
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logging.basicConfig(level=logging.INFO)

# ─── 환경변수 ─────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
WEBHOOK_URL     = os.environ["WEBHOOK_URL"]
TAVILY_API_KEY  = os.environ["TAVILY_API_KEY"]
RPC_URL         = os.environ["RPC_URL"]
BOT_PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY"]
TOKEN_ADDRESS   = os.environ["TOKEN_ADDRESS"]

# ─── Web3 초기화 ──────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(RPC_URL))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
BOT_WALLET  = w3.eth.account.from_key(BOT_PRIVATE_KEY)
BOT_ADDRESS = BOT_WALLET.address

# ─── 주소 (TX에서 직접 확인된 값) ────────────────────────────────
WMATIC   = w3.to_checksum_address("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270")
TOKEN    = w3.to_checksum_address(TOKEN_ADDRESS)

# QuickSwap V2 Router (TX에서 확인)
QS_ROUTER = w3.to_checksum_address("0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff")

# Uniswap V2 Router (Polygon)
UNI_ROUTER = w3.to_checksum_address("0xedf6066a2b290C185783862C7F4776A2C8077AD1")

# ─── ABI ─────────────────────────────────────────────────────────
ROUTER_ABI = json.loads('[{"inputs":[{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactETHForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"payable","type":"function"},{"inputs":[{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactETHForTokensSupportingFeeOnTransferTokens","outputs":[],"stateMutability":"payable","type":"function"},{"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForETHSupportingFeeOnTransferTokens","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForETH","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"}]')

ERC20_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"}],"name":"allowance","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')

# ─── 설정 ─────────────────────────────────────────────────────────
MIN_POL = 0.5
MAX_POL = 2.0

groq_client          = Groq(api_key=GROQ_API_KEY)
tavily_client        = TavilyClient(api_key=TAVILY_API_KEY)
conversation_history = {}
trading_active       = False
trading_thread       = None
app                  = Flask(__name__)

SEARCH_KEYWORDS = ["현재", "지금", "오늘", "최신", "최근", "주가", "날씨", "뉴스", "환율", "가격", "몇시", "누구야", "대통령", "총리", "결과"]

def needs_search(text):
    return any(k in text for k in SEARCH_KEYWORDS)

def get_random_pol_amount():
    return round(random.uniform(MIN_POL, MAX_POL), random.randint(4, 7))

def get_nonce():
    return w3.eth.get_transaction_count(BOT_ADDRESS, 'pending')

def get_gas_price():
    # TX에서 확인된 가스비: 410 Gwei → 여유있게 500 Gwei 이상
    base = w3.eth.gas_price
    return max(int(base * 1.5), w3.to_wei(500, 'gwei'))

def ensure_approved(token_contract, spender, amount, nonce, gas_price):
    allowance = token_contract.functions.allowance(BOT_ADDRESS, spender).call()
    if allowance >= amount:
        return nonce
    approve_tx = token_contract.functions.approve(
        spender, 2**256 - 1
    ).build_transaction({
        'from': BOT_ADDRESS, 'gas': 100000,
        'gasPrice': gas_price, 'nonce': nonce,
    })
    signed  = w3.eth.account.sign_transaction(approve_tx, BOT_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    logging.info(f"Approve 완료: {tx_hash.hex()}")
    return nonce + 1

# ─── 매수: QuickSwap (POL → ELAHZ) ───────────────────────────────
def buy_elahz_quickswap(pol_amount):
    try:
        router    = w3.eth.contract(address=QS_ROUTER, abi=ROUTER_ABI)
        amount_in = w3.to_wei(pol_amount, 'ether')
        deadline  = int(datetime.now().timestamp()) + 300
        nonce     = get_nonce()
        gas_price = get_gas_price()
        path      = [WMATIC, TOKEN]

        # Tax 토큰은 SupportingFeeOnTransferTokens 함수 사용
        tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            0, path, BOT_ADDRESS, deadline
        ).build_transaction({
            'from': BOT_ADDRESS, 'value': amount_in,
            'gas': 300000, 'gasPrice': gas_price, 'nonce': nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logging.info(f"QuickSwap 매수 TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        return tx_hash.hex(), receipt.status

    except Exception as e:
        logging.error(f"QuickSwap 매수 오류: {e}")
        return None, 0

# ─── 매수: Uniswap V2 (POL → ELAHZ) ─────────────────────────────
def buy_elahz_uniswap(pol_amount):
    try:
        router    = w3.eth.contract(address=UNI_ROUTER, abi=ROUTER_ABI)
        amount_in = w3.to_wei(pol_amount, 'ether')
        deadline  = int(datetime.now().timestamp()) + 300
        nonce     = get_nonce()
        gas_price = get_gas_price()
        path      = [WMATIC, TOKEN]

        tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            0, path, BOT_ADDRESS, deadline
        ).build_transaction({
            'from': BOT_ADDRESS, 'value': amount_in,
            'gas': 300000, 'gasPrice': gas_price, 'nonce': nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logging.info(f"Uniswap 매수 TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        return tx_hash.hex(), receipt.status

    except Exception as e:
        logging.error(f"Uniswap 매수 오류: {e}")
        return None, 0

# ─── 매도: QuickSwap (ELAHZ → POL) ──────────────────────────────
def sell_elahz_quickswap():
    try:
        token     = w3.eth.contract(address=TOKEN, abi=ERC20_ABI)
        router    = w3.eth.contract(address=QS_ROUTER, abi=ROUTER_ABI)
        gas_price = get_gas_price()

        balance   = token.functions.balanceOf(BOT_ADDRESS).call()
        amount_in = int(balance * random.uniform(0.4, 0.6))
        if amount_in == 0:
            return None, 0

        nonce    = get_nonce()
        deadline = int(datetime.now().timestamp()) + 300
        path     = [TOKEN, WMATIC]

        nonce = ensure_approved(token, QS_ROUTER, amount_in, nonce, gas_price)

        tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amount_in, 0, path, BOT_ADDRESS, deadline
        ).build_transaction({
            'from': BOT_ADDRESS, 'value': 0,
            'gas': 300000, 'gasPrice': gas_price, 'nonce': nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logging.info(f"QuickSwap 매도 TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        return tx_hash.hex(), receipt.status

    except Exception as e:
        logging.error(f"QuickSwap 매도 오류: {e}")
        return None, 0

# ─── 매도: Uniswap V2 (ELAHZ → POL) ─────────────────────────────
def sell_elahz_uniswap():
    try:
        token     = w3.eth.contract(address=TOKEN, abi=ERC20_ABI)
        router    = w3.eth.contract(address=UNI_ROUTER, abi=ROUTER_ABI)
        gas_price = get_gas_price()

        balance   = token.functions.balanceOf(BOT_ADDRESS).call()
        amount_in = int(balance * random.uniform(0.4, 0.6))
        if amount_in == 0:
            return None, 0

        nonce    = get_nonce()
        deadline = int(datetime.now().timestamp()) + 300
        path     = [TOKEN, WMATIC]

        nonce = ensure_approved(token, UNI_ROUTER, amount_in, nonce, gas_price)

        tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amount_in, 0, path, BOT_ADDRESS, deadline
        ).build_transaction({
            'from': BOT_ADDRESS, 'value': 0,
            'gas': 300000, 'gasPrice': gas_price, 'nonce': nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logging.info(f"Uniswap 매도 TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        return tx_hash.hex(), receipt.status

    except Exception as e:
        logging.error(f"Uniswap 매도 오류: {e}")
        return None, 0

# ─── 자동거래 루프 (별도 스레드) ─────────────────────────────────
def trading_loop(chat_id):
    global trading_active

    async def _run():
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            await bot.send_message(chat_id=chat_id, text="🤖 자동거래 루프 시작!\nQuickSwap + Uniswap V2 동시 운영")

            while trading_active:
                try:
                    pol_amount = get_random_pol_amount()
                    # QuickSwap / Uniswap 번갈아가며 실행
                    use_quickswap = random.choice([True, False])
                    dex_name = "QuickSwap" if use_quickswap else "Uniswap V2"

                    # ── 매수 ─────────────────────────────────────
                    await bot.send_message(chat_id=chat_id, text=f"🔄 [{dex_name}] 매수 시도... {pol_amount} POL")
                    await asyncio.sleep(random.randint(1, 5))

                    if use_quickswap:
                        tx_hash, status = await asyncio.get_event_loop().run_in_executor(None, buy_elahz_quickswap, pol_amount)
                    else:
                        tx_hash, status = await asyncio.get_event_loop().run_in_executor(None, buy_elahz_uniswap, pol_amount)

                    if status == 1:
                        await bot.send_message(chat_id=chat_id, text=f"✅ [{dex_name}] 매수 완료\nPOL: {pol_amount}\nhttps://polygonscan.com/tx/{tx_hash}")
                    else:
                        await bot.send_message(chat_id=chat_id, text=f"❌ [{dex_name}] 매수 실패\nTX: {tx_hash}")

                    # 매수 후 대기 25~35분
                    wait = random.randint(1500, 2100)
                    await bot.send_message(chat_id=chat_id, text=f"⏳ {wait//60}분 후 매도 예정")
                    await asyncio.sleep(wait)

                    if not trading_active:
                        break

                    # ── 매도 ─────────────────────────────────────
                    await bot.send_message(chat_id=chat_id, text=f"🔄 [{dex_name}] 매도 시도...")
                    await asyncio.sleep(random.randint(1, 5))

                    if use_quickswap:
                        tx_hash, status = await asyncio.get_event_loop().run_in_executor(None, sell_elahz_quickswap)
                    else:
                        tx_hash, status = await asyncio.get_event_loop().run_in_executor(None, sell_elahz_uniswap)

                    if status == 1:
                        await bot.send_message(chat_id=chat_id, text=f"✅ [{dex_name}] 매도 완료\nhttps://polygonscan.com/tx/{tx_hash}")
                    else:
                        await bot.send_message(chat_id=chat_id, text=f"❌ [{dex_name}] 매도 실패\nTX: {tx_hash}")

                    # 다음 거래까지 대기 25~35분
                    wait = random.randint(1500, 2100)
                    await bot.send_message(chat_id=chat_id, text=f"⏳ {wait//60}분 후 다음 거래 예정")
                    await asyncio.sleep(wait)

                except Exception as e:
                    logging.error(f"자동거래 오류: {e}")
                    try:
                        await bot.send_message(chat_id=chat_id, text=f"⚠️ 오류: {str(e)}\n60초 후 재시도")
                    except:
                        pass
                    await asyncio.sleep(60)

            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 루프 종료")

    asyncio.run(_run())

# ─── 텔레그램 핸들러 ──────────────────────────────────────────────
async def handle_update(update_data):
    global trading_active, trading_thread
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
                "안녕하세요!\n\n📈 거래 명령어:\n"
                "/starttrading - 자동거래 시작 (QuickSwap+Uniswap)\n"
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
            trading_thread = threading.Thread(target=trading_loop, args=(chat_id,), daemon=True)
            trading_thread.start()
            await bot.send_message(chat_id=chat_id, text="✅ 자동거래 시작!")
            return

        if user_text == "/stoptrading":
            trading_active = False
            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 중지되었습니다.")
            return

        if user_text == "/balance":
            try:
                pol_bal   = w3.eth.get_balance(BOT_ADDRESS)
                pol       = w3.from_wei(pol_bal, 'ether')
                token_c   = w3.eth.contract(address=TOKEN, abi=ERC20_ABI)
                elahz_bal = token_c.functions.balanceOf(BOT_ADDRESS).call()
                elahz     = elahz_bal / 10**18
                await bot.send_message(chat_id=chat_id, text=f"💰 봇 지갑 잔액\nPOL: {pol:.6f}\nELAHZ: {elahz:.4f}")
            except Exception as e:
                await bot.send_message(chat_id=chat_id, text=f"잔액 조회 오류: {str(e)}")
            return

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
                messages=[{"role": "system", "content": "You are a helpful assistant. Respond in the same language the user uses."}] + conversation_history[user_id],
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
    return "Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
