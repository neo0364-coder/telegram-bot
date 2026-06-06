import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update, Bot
from groq import Groq
from tavily import TavilyClient

logging.basicConfig(level=logging.INFO)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]

groq_client = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

conversation_history = {}
app = Flask(__name__)

SEARCH_KEYWORDS = ["현재", "지금", "오늘", "최신", "최근", "주가", "날씨", "뉴스", "환율", "가격", "몇시", "누구야", "대통령", "총리", "결과"]

def needs_search(text):
    return any(keyword in text for keyword in SEARCH_KEYWORDS)

async def handle_update(update_data):
    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        update = Update.de_json(update_data, bot)
        if not update.message or not update.message.text:
            return
        user_id = update.effective_user.id
        user_text = update.message.text

        if user_text == "/start":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=update.message.chat_id, text="안녕하세요! 무엇이든 물어보세요.\n/reset 으로 대화 초기화 가능합니다.")
            return

        if user_text == "/reset":
            conversation_history[user_id] = []
            await bot.send_message(chat_id=update.message.chat_id, text="대화 기록이 초기화되었습니다.")
            return

        if user_id not in conversation_history:
            conversation_history[user_id] = []

        # 검색이 필요한 경우 Tavily로 검색
        search_context = ""
        if needs_search(user_text):
            try:
                search_result = tavily_client.search(query=user_text, max_results=3)
                search_context = "\n\n[검색 결과]\n"
                for r in search_result["results"]:
                    search_context += f"- {r['title']}: {r['content'][:200]}\n"
            except Exception as e:
                logging.error(f"검색 오류: {e}")

        # 검색 결과를 포함해서 질문 구성
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

        await bot.send_message(chat_id=update.message.chat_id, text=reply)

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
