import os
import asyncio
import base64
import io
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from openai import AsyncOpenAI

BOT_TOKEN = "8662325806:AAEj9zw70aEEJX52tlaUkutu7jQkZ3gbNdE"
OPENAI_API_KEY =  "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"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
user_history = defaultdict(list)

IMAGE_PROMPT_TEMPLATE = "cinematic DSLR style, ultra-realistic face detail, dramatic lighting, shallow depth of field, 8k resolution, professional photography: {}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Image Mode", callback_data="mode_image"),
         InlineKeyboardButton("Video Mode", callback_data="mode_video")],
        [InlineKeyboardButton("History", callback_data="history"),
         InlineKeyboardButton("Reset", callback_data="reset")]
    ]
    await update.message.reply_text("Select mode:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "mode_image":
        context.user_data["mode"] = "image"
        await query.edit_message_text("Image mode active. Send a prompt.")

    elif query.data == "mode_video":
        context.user_data["mode"] = "video"
        await query.edit_message_text("Video mode active. Send a prompt.")

    elif query.data == "history":
        history = user_history.get(user_id, [])
        if not history:
            await query.edit_message_text("No history.")
        else:
            text = "Last prompts:\n" + "\n".join([f"{i+1}. {h}" for i, h in enumerate(history[-5:])])
            await query.edit_message_text(text)

    elif query.data == "reset":
        user_history[user_id] = []
        context.user_data["mode"] = None
        await query.edit_message_text("History cleared.")

async def generate_images(prompt: str):
    enhanced = IMAGE_PROMPT_TEMPLATE.format(prompt)

    response = await client.images.generate(
        model="gpt-image-1",
        prompt=enhanced,
        n=3,
        size="1024x1024"
    )

    images = []
    for img in response.data:
        decoded = base64.b64decode(img.b64_json)
        bio = io.BytesIO(decoded)
        bio.name = "image.png"
        images.append(bio)

    return images

async def generate_video(prompt: str):
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text

    if not text or not text.strip():
        await update.message.reply_text("Please send a valid prompt.")
        return

    mode = context.user_data.get("mode", "image")

    user_history[user_id].append(text)
    if len(user_history[user_id]) > 50:
        user_history[user_id] = user_history[user_id][-50:]

    progress = await update.message.reply_text("Starting...")

    try:
        if mode == "image":
            await progress.edit_text("Enhancing prompt...")
            await asyncio.sleep(0.5)

            await progress.edit_text("Generating...")
            images = await generate_images(text)

            await progress.edit_text("Sending results...")

            media = [InputMediaPhoto(img) for img in images]
            await update.message.reply_media_group(media=media)

            await progress.delete()

        elif mode == "video":
            await progress.edit_text("Generating video...")
            await asyncio.sleep(0.5)

            await progress.edit_text("Video generation is currently unavailable.")

    except Exception as e:
        await progress.edit_text(f"Error: {str(e)}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"GLOBAL ERROR: {context.error}")
    if update and update.message:
        await update.message.reply_text(f"Error: {str(context.error)}")

def main():
    if not BOT_TOKEN or not OPENAI_API_KEY:
        raise ValueError("Missing BOT_TOKEN or OPENAI_API_KEY")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()
