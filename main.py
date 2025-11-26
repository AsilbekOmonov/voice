from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters)
from telegram import Update
from openai import OpenAI
import os
import tempfile
from dotenv import load_dotenv
import json
import re

# Tokens and keys are left as in the original file (check before running)
load_dotenv()
Token = os.getenv("Token")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# USD to UZS exchange rate (updated based on current rate, ~11964.2 in November 2025)
# USD_TO_UZS = 11964.2
USD_TO_UZS = 20000.2
# USD_TO_UZS = 119640.2

# Simple billing structure
billing = {
    "entries": [],   # list of entries: { "model": ..., "raw_sum": float, "sum_uzs": float, "tokens": int, "note": ... }
    "total_raw": 0.0,
    "total_uzs": 0.0,
    "total_tokens": 0
}

# Function to add a billing entry
def add_billing_entry(model_name: str, cost_usd: float, tokens: int = 0, note: str = ""):
    uzs = cost_usd * USD_TO_UZS
    entry = {
        "model": model_name,
        "raw_sum": cost_usd,
        "sum_uzs": uzs,
        "tokens": tokens,
        "note": note
    }
    billing["entries"].append(entry)
    billing["total_raw"] += cost_usd
    billing["total_uzs"] += uzs
    billing["total_tokens"] += tokens
    return entry

# Function for formatted balance output (command /balance)
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not billing["entries"]:
        await update.message.reply_text("Balance is empty. No entries.")
        return

    lines = []
    lines.append("Billing summary:")
    for i, e in enumerate(billing["entries"], 1):
        lines.append(f"{i}. Model: {e['model']}. Raw {e['raw_sum']:.4f}$, UZS {e['sum_uzs']:.2f}; Tokens: {e['tokens']}; {e.get('note','')}")
    lines.append("────────────────────")
    lines.append(f"Total raw: {billing['total_raw']:.4f}$")
    lines.append(f"Total in UZS: {billing['total_uzs']:.2f} UZS")
    lines.append(f"Total tokens: {billing['total_tokens']}")

    await update.message.reply_text("\n".join(lines))

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    from datetime import datetime
    error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n‼️[{error_time}] Error in Telegram bot:")
    print("-" * 80)
    print(f"Error type: {type(context.error).__name__}")
    print(f"Description: {context.error}")
    print("Stack trace:")
    traceback.print_exc()
    print("-" * 80)
    msg = "⚠️ An error occurred. Please try again."
    if update and update.callback_query:
        await update.callback_query.message.reply_text(msg)
    elif update and update.message:
        await update.message.reply_text(msg)

# Function to handle voice messages
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    if voice:
        # Download voice message as OGG
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as temp_ogg:
            await file.download_to_drive(temp_ogg.name)
            ogg_path = temp_ogg.name

        try:
            # Transcribe speech using OpenAI Whisper API (supports OGG directly)
            with open(ogg_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="en"  # For English speech
                )
            text = transcription.text
            await update.message.reply_text(f"Recognized text: {text}")

            # Calculate Whisper cost (0.006 USD per minute)
            duration_sec = voice.duration
            whisper_cost = (duration_sec / 60.0) * 0.006
            whisper_added = add_billing_entry("whisper-1", whisper_cost, tokens=0, note=f"voice duration: {duration_sec} seconds")
            await update.message.reply_text(f"Added billing for Whisper: raw {whisper_added['raw_sum']:.4f}$, UZS {whisper_added['sum_uzs']:.2f}")

            # Break text into unique words in order of appearance (ignore punctuation, only >3 letters)
            words = []
            seen = set()
            for word in re.findall(r'\b\w+\b', text.lower()):
                if len(word) > 3 and word not in seen:
                    seen.add(word)
                    words.append(word)

            # List of words to send to GPT
            word_list = []
            total_prompt_tokens = 0
            total_completion_tokens = 0
            total_tokens = 0
            model_name = "gpt-4.1-mini"

            for word in words:
                # Request translation (to Uzbek) and definition (in Uzbek) with context from GPT
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": "You are a translator and dictionary. For the given English word in the context of the full text, provide: translate (to Uzbek, considering context), definition (in Uzbek, considering context). Respond only in JSON: {'translate':'...','definition':'...'}"},
                        {"role": "user", "content": f"Word: {word}\nFull text context: {text}"}
                    ]
                )
                # Attempt to parse JSON response
                try:
                    data = json.loads(response.choices[0].message.content)
                except Exception:
                    # If GPT returned invalid JSON, skip the word
                    continue

                word_dict = {
                    "word": word,
                    "translate": data.get("translate", "Not found"),
                    "definition": data.get("definition", "Not found")
                }
                word_list.append(word_dict)

                # Accumulate used tokens
                try:
                    total_prompt_tokens += response.usage.prompt_tokens
                    total_completion_tokens += response.usage.completion_tokens
                    total_tokens += response.usage.total_tokens
                except Exception:
                    pass

            # Send results to the user
            for word_dict in word_list:
                await update.message.reply_text(
                    "────────────────────\n"
                    f"<b>{word_dict['word']}</b> → <i>{word_dict['translate']}</i>\n"
                    f"<i>definition: {word_dict['definition']}</i>",
                    parse_mode="HTML"
                )

            # Calculate GPT cost (no cache, standard prices)
            input_price_per_token = 0.40 / 1000000.0
            output_price_per_token = 1.60 / 1000000.0
            gpt_cost = (total_prompt_tokens * input_price_per_token) + (total_completion_tokens * output_price_per_token)
            gpt_added = add_billing_entry(model_name, gpt_cost, tokens=total_tokens, note="per voice analysis")
            await update.message.reply_text(f"Total tokens used for analysis: {total_tokens}\nAdded billing for GPT: raw {gpt_added['raw_sum']:.4f}$, UZS {gpt_added['sum_uzs']:.2f}")

        except Exception as e:
            await update.message.reply_text(f"Error in recognition: {str(e)}")

        finally:
            # Remove temporary file
            try:
                os.remove(ogg_path)
            except Exception:
                pass

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Send me a voice message, and I'll try to recognize and analyze it.")

# Asynchronous function for post_init
async def post_init(app):
    print("✅ Bot started")

# Create and run the bot
app = (
    ApplicationBuilder()
    .token(Token)
    .post_init(post_init)
    .build()
)

# Register handlers
app.add_error_handler(error_handler)
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("balance", balance_command))

if __name__ == "__main__":
    app.run_polling()