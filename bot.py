import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update, Bot
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

client = genai.Client(api_key=GEMINI_API_KEY)

conversation_history = {}
app = Flask(__name__)

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

        conversation_history[user_id].append(types.Content(role="user", parts=[types.Part(text=user_text)]))

        if len(conversation_history[user_id]) > 20:
            conversation_history[user_id] = conversation_history[user_id][-20:]

        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=conversation_history[user_id],
                config=types.GenerateContentConfig(
                    system_instruction="You are a helpful assistant. Respond in the same language the user uses.",
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                )
            )
            reply = response.text
            conversation_history[user_id].append(types.Content(role="model", parts=[types.Part(text=reply)]))
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
