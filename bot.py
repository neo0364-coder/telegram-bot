import os
import logging
import asyncio
import random
import json
from datetime import datetime, time
from flask import Flask, request
from telegram import Update, Bot
from groq import Groq
from tavily import TavilyClient
from web3 import Web3

logging.basicConfig(level=logging.INFO)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
RPC_URL = os.environ["RPC_URL"]
BOT_PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY"]
TOKEN_ADDRESS = os.environ["TOKEN_ADDRESS"]
POOL_ADDRESS = os.environ["POOL_ADDRESS"]

groq_client = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
w3 = Web3(Web3.HTTPProvider(RPC_URL))

BOT_WALLET = w3.eth.account.from_key(BOT_PRIVATE_KEY)
BOT_ADDRESS = BOT_WALLET.address

# Aerodrome Router 주소 (Base)
ROUTER_ADDRESS = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"

ROUTER_ABI = json.loads('[{"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactETHForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"payable","type":"function"},{"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForETH","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"name":"poolFor","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"view","type":"function"}]')

ERC20_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"}],"name":"allowance","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')

WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
FACTORY_ADDRESS = "0x420DD381b31aEf6683db6B902084cB0FFECe40D"

# 거래 범위 설정
MIN_ETH = 0.0001
MAX_ETH = 0.001

conversation_history = {}
trading_active = False
app = Flask(__name__)

SEARCH_KEYWORDS = ["현재", "지금", "오늘", "최신", "최근", "주가", "날씨", "뉴스", "환율", "가격", "몇시", "누구야", "대통령", "총리", "결과"]

def needs_search(text):
    return any(keyword in text for keyword in SEARCH_KEYWORDS)

def get_random_eth_amount():
    amount = random.uniform(MIN_ETH, MAX_ETH)
    # 소수점 랜덤하게 해서 봇처럼 안 보이게
    decimals = random.randint(4, 7)
    return round(amount, decimals)

def buy_elahs(eth_amount):
    try:
        router = w3.eth.contract(address=w3.to_checksum_address(ROUTER_ADDRESS), abi=ROUTER_ABI)
        token = w3.eth.contract(address=w3.to_checksum_address(TOKEN_ADDRESS), abi=ERC20_ABI)
        
        amount_in = w3.to_wei(eth_amount, 'ether')
        deadline = int(datetime.now().timestamp()) + 300
        
        routes = [{
            "from": w3.to_checksum_address(WETH_ADDRESS),
            "to": w3.to_checksum_address(TOKEN_ADDRESS),
            "stable": False,
            "factory": w3.to_checksum_address(FACTORY_ADDRESS)
        }]
        
        nonce = w3.eth.get_transaction_count(BOT_ADDRESS)
        gas_price = w3.eth.gas_price
        
        tx = router.functions.swapExactETHForTokens(
            0,
            routes,
            BOT_ADDRESS,
            deadline
        ).build_transaction({
            'from': BOT_ADDRESS,
            'value': amount_in,
            'gas': 300000,
            'gasPrice': gas_price,
            'nonce': nonce,
        })
        
        signed = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        return tx_hash.hex(), receipt.status
    except Exception as e:
        logging.error(f"매수 오류: {e}")
        return None, 0

def sell_elahs(eth_amount):
    try:
        router = w3.eth.contract(address=w3.to_checksum_address(ROUTER_ADDRESS), abi=ROUTER_ABI)
        token = w3.eth.contract(address=w3.to_checksum_address(TOKEN_ADDRESS), abi=ERC20_ABI)
        
        # 현재 ELAHS 잔액의 일부만 매도 (ETH 금액 기준으로 계산)
        elahs_balance = token.functions.balanceOf(BOT_ADDRESS).call()
        amount_in = int(elahs_balance * random.uniform(0.3, 0.7))
        
        if amount_in == 0:
            return None, 0
        
        deadline = int(datetime.now().timestamp()) + 300
        nonce = w3.eth.get_transaction_count(BOT_ADDRESS)
        gas_price = w3.eth.gas_price
        
        # approve 먼저
        allowance = token.functions.allowance(BOT_ADDRESS, w3.to_checksum_address(ROUTER_ADDRESS)).call()
        if allowance < amount_in:
            approve_tx = token.functions.approve(
                w3.to_checksum_address(ROUTER_ADDRESS),
                2**256 - 1
            ).build_transaction({
                'from': BOT_ADDRESS,
                'gas': 100000,
                'gasPrice': gas_price,
                'nonce': nonce,
            })
            signed_approve = w3.eth.account.sign_transaction(approve_tx, BOT_PRIVATE_KEY)
            w3.eth.send_raw_transaction(signed_approve.raw_transaction)
            w3.eth.wait_for_transaction_receipt(signed_approve.raw_transaction)
            nonce += 1
        
        routes = [{
            "from": w3.to_checksum_address(TOKEN_ADDRESS),
            "to": w3.to_checksum_address(WETH_ADDRESS),
            "stable": False,
            "factory": w3.to_checksum_address(FACTORY_ADDRESS)
        }]
        
        tx = router.functions.swapExactTokensForETH(
            amount_in,
            0,
            routes,
            BOT_ADDRESS,
            deadline
        ).build_transaction({
            'from': BOT_ADDRESS,
            'gas': 300000,
            'gasPrice': gas_price,
            'nonce': nonce,
        })
        
        signed = w3.eth.account.sign_transaction(tx, BOT_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        return tx_hash.hex(), receipt.status
    except Exception as e:
        logging.error(f"매도 오류: {e}")
        return None, 0

async def auto_trade(bot, chat_id):
    global trading_active
    while trading_active:
        try:
            eth_amount = get_random_eth_amount()
            
            # 매수
            await asyncio.sleep(random.randint(1, 10))
            tx_hash, status = buy_elahs(eth_amount)
            if status == 1:
                await bot.send_message(chat_id=chat_id, text=f"✅ 매수 완료\nETH: {eth_amount}\nhttps://basescan.org/tx/{tx_hash}")
            else:
                await bot.send_message(chat_id=chat_id, text=f"❌ 매수 실패")
            
            # 매수 후 랜덤 대기 (1~6시간)
            wait = random.randint(3600, 21600)
            await asyncio.sleep(wait)
            
            if not trading_active:
                break
            
            # 매도
            await asyncio.sleep(random.randint(1, 10))
            tx_hash, status = sell_elahs(eth_amount)
            if status == 1:
                await bot.send_message(chat_id=chat_id, text=f"✅ 매도 완료\nhttps://basescan.org/tx/{tx_hash}")
            else:
                await bot.send_message(chat_id=chat_id, text=f"❌ 매도 실패")
            
            # 다음 거래까지 랜덤 대기 (1~6시간)
            wait = random.randint(3600, 21600)
            await asyncio.sleep(wait)
            
        except Exception as e:
            logging.error(f"자동거래 오류: {e}")
            await asyncio.sleep(60)

async def handle_update(update_data):
    global trading_active
    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        update = Update.de_json(update_data, bot)
        if not update.message or not update.message.text:
            return
        user_id = update.effective_user.id
        chat_id = update.message.chat_id
        user_text = update.message.text

        if user_text == "/start":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=chat_id, text="안녕하세요! 무엇이든 물어보세요.\n\n📈 거래 명령어:\n/starttrading - 자동거래 시작\n/stoptrading - 자동거래 중지\n/balance - 잔액 확인\n/reset - 대화 초기화")
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
            await bot.send_message(chat_id=chat_id, text="✅ 자동거래 시작!\n하루 2회 랜덤 매수/매도 진행합니다.")
            asyncio.create_task(auto_trade(bot, chat_id))
            return

        if user_text == "/stoptrading":
            trading_active = False
            await bot.send_message(chat_id=chat_id, text="⛔ 자동거래 중지되었습니다.")
            return

        if user_text == "/balance":
            try:
                eth_balance = w3.eth.get_balance(BOT_ADDRESS)
                eth = w3.from_wei(eth_balance, 'ether')
                token = w3.eth.contract(address=w3.to_checksum_address(TOKEN_ADDRESS), abi=ERC20_ABI)
                elahs_balance = token.functions.balanceOf(BOT_ADDRESS).call()
                elahs = elahs_balance / 10**18
                await bot.send_message(chat_id=chat_id, text=f"💰 봇 지갑 잔액\nETH: {eth:.6f}\nELAHS: {elahs:.2f}")
            except Exception as e:
                await bot.send_message(chat_id=chat_id, text=f"잔액 조회 오류: {str(e)}")
            return

        if user_id not in conversation_history:
            conversation_history[user_id] = []

        search_context = ""
        if needs_search(user_text):
            try:
                search_result = tavily_client.search(query=user_text, max_results=3)
                search_context = "\n\n[검색 결과]\n"
                for r in search_result["results"]:
                    search_context += f"- {r['title']}: {r['content'][:200]}\n"
            except Exception as e:
                logging.error(f"검색 오류: {e}")

        augmented_text = user_text
        if search_context:
            augmented_text = f"{user_text}\n{search_context}"

        conversation_history[user_id].append({"role": "user", "content": augmented_text})

        if len(conversation_history[user_id]) > 20:
            conversation_history[user_id] = conversation_history[user_id][-20:]

        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant. Respond in the same language the user uses. If search results are provided, use them to give accurate and up-to-date answers."}
                ] + conversation_history[user_id],
                max_tokens=1024,
            )
            reply = response.choices[0].message.content
            conversation_history[user_id].append({"role": "assistant", "content": reply})
        except Exception as e:
            reply = f"오류가 발생했습니다: {str(e)}"

        await bot.send_message(chat_id=chat_id, text=reply)

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
