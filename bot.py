import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

groq_client = Groq(api_key=GROQ_API_KEY)
conversation_history = {}
app = Flask(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    await update.message.reply_text("안녕하세요! 무엇이든 물어보세요.\n/reset 으로 대화 초기화 가능합니다.")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    await update.message.reply_text("대화 기록이 초기화되었습니다.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({"role": "user", "content": user_text})
    if len(conversation_history[user_id]) > 10:
        conversation_history[user_id] = conversation_history[user_id][-10:]
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
    await update.message.reply_text(reply)

@app.route(f"/bot{os.environ.get('TELEGRAM_TOKEN', '')}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    async def process():
        async with ptb_app:
            update = Update.de_json(update_data, ptb_app.bot)
            await ptb_app.process_update(update)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process())
    finally:
        loop.close()
    return "OK"

@app.route("/set_webhook")
def set_webhook():
    async def _set():
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            await bot.set_webhook(f"{WEBHOOK_URL}/bot{TELEGRAM_TOKEN}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_set())
    finally:
        loop.close()
    return f"Webhook set to {WEBHOOK_URL}/bot{TELEGRAM_TOKEN}"

@app.route("/")
def index():
    return "Bot is running!"

ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("reset", reset))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
