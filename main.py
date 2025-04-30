import requests
from bs4 import BeautifulSoup # Added back for HTML cleaning
import schedule
import time
import sqlite3
import json
import os
from openai import OpenAI
from urllib.parse import urljoin # Keep for potential relative links in feeds? Maybe not needed. Let's keep for now.
import logging
from dotenv import load_dotenv
# Removed: import newspaper # No longer using newspaper3k
import feedparser # Added feedparser
import socket # Ensure socket is imported globally if used in both functions
import asyncio # Added asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto # Added telegram components
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue # Added telegram extensions
from telegram.constants import ParseMode
from telegram.error import TelegramError
import html # Added for unescaping HTML entities
import base64 # Added for image processing
import random # Added for selecting random feed in /test

# --- Configuration ---
load_dotenv() # Load environment variables from .env file

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s' # Added logger name
)
# Set higher logging level for httpx to avoid verbose DEBUG messages
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__) # Use specific logger

# --- Environment Variables & Constants ---
# List of RSS Feed URLs to scrape
# Example: NEWS_URLS='["http://feeds.bbci.co.uk/news/technology/rss.xml", "https://techcrunch.com/feed/"]'
NEWS_URLS = json.loads(os.getenv('NEWS_URLS', '[]')) # Default to empty list if not set
WEBHOOK_URL = os.getenv('WEBHOOK_URL') # Your webhook URL
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# --- OpenAI Image Generation Config ---
# NOTE: OPENAI_API_KEY is now REQUIRED if IMAGE_GENERATION_ENABLED is True
# It will always use the official OpenAI endpoint for images.
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY') # Removed fallback to OPENROUTER_API_KEY here
OPENAI_IMAGE_MODEL = os.getenv('OPENAI_IMAGE_MODEL', "gpt-image-1") # Model for image generation
IMAGE_GENERATION_ENABLED = os.getenv('IMAGE_GENERATION_ENABLED', 'True').lower() == 'true'
IMAGE_SIZE = os.getenv('IMAGE_SIZE', '1024x1024') # e.g., 1024x1024, 1024x1536, 1536x1024
IMAGE_QUALITY = os.getenv('IMAGE_QUALITY', 'medium') # e.g., standard, hd

# --- Catbox Config ---
CATBOX_API_URL = "https://catbox.moe/user/api.php"
CATBOX_USER_HASH = os.getenv('CATBOX_USER_HASH', '') # Optional: Set if you have a Catbox user hash

GPT_MODEL = os.getenv('GPT_MODEL', "openai/gpt-4o-mini") # Changed default model slightly
DATABASE_NAME = 'news_log.db'
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_MINUTES', 15)) * 60
MAX_RECENT_ARTICLES_FOR_CHECK = int(os.getenv('MAX_RECENT_ARTICLES_FOR_CHECK', 30)) # Increased default slightly
# Removed: ARTICLE_DOWNLOAD_TIMEOUT # Not directly applicable to feedparser in the same way
# Removed: MAX_ARTICLES_PER_SOURCE # feedparser gives all entries, we process them
FEED_REQUEST_TIMEOUT = int(os.getenv('FEED_REQUEST_TIMEOUT', 30)) # Timeout for fetching the feed itself
WEBHOOK_TIMEOUT = int(os.getenv('WEBHOOK_TIMEOUT', 45))
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# Content Limits
TELEGRAM_CAPTION_LIMIT = 1024 # Telegram caption limit
SUMMARY_PREVIEW_LIMIT = 700 # Limit summary length in Telegram message initially

# --- Newspaper3k Configuration ---
# Removed newspaper3k config section

# --- OpenAI Client Setup ---
# Client for Text/Deduplication (using OpenRouter)
openrouter_client = None
if OPENROUTER_API_KEY:
    openrouter_client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
    )
else:
    logging.warning("OPENROUTER_API_KEY not found. Deduplication feature will be disabled.")

# Client for Image Generation (using official OpenAI API)
image_client = None
if IMAGE_GENERATION_ENABLED:
    if OPENAI_API_KEY:
        logger.info(f"Image generation enabled. Using official OpenAI endpoint with model {OPENAI_IMAGE_MODEL}.")
        # Initialize with only api_key to use the default OpenAI base URL
        image_client = OpenAI(api_key=OPENAI_API_KEY)
    else:
        logger.warning("IMAGE_GENERATION_ENABLED is True, but OPENAI_API_KEY is not set. Disabling image generation.")
        IMAGE_GENERATION_ENABLED = False # Force disable
else:
    logger.info("Image generation is disabled via environment variable.")

# --- Database Functions ---
def init_db():
    """Initializes the SQLite database and adds new columns if they don't exist."""
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,         -- Added summary column
                    image_url TEXT,     -- Added image_url column
                    source_feed TEXT,   -- Added source_feed column
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Add columns if they don't exist (for backward compatibility)
            try:
                cursor.execute("ALTER TABLE articles ADD COLUMN summary TEXT")
                logger.info("Added 'summary' column to articles table.")
            except sqlite3.OperationalError: pass # Column already exists
            try:
                cursor.execute("ALTER TABLE articles ADD COLUMN image_url TEXT")
                logger.info("Added 'image_url' column to articles table.")
            except sqlite3.OperationalError: pass # Column already exists
            try:
                cursor.execute("ALTER TABLE articles ADD COLUMN source_feed TEXT")
                logger.info("Added 'source_feed' column to articles table.")
            except sqlite3.OperationalError: pass # Column already exists

            conn.commit()
        logger.info(f"Database '{DATABASE_NAME}' initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database error during initialization: {e}", exc_info=True)
        raise

# Wrap DB operations in asyncio.to_thread for async context
async def article_exists_async(url):
    """Checks if an article URL exists in the database asynchronously."""
    return await asyncio.to_thread(article_exists, url)

def article_exists(url):
    """Checks if an article with the given URL already exists in the database (blocking)."""
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM articles WHERE url = ?", (url,))
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logger.error(f"Database error checking article existence for {url}: {e}", exc_info=True)
        return True # Assume exists on error

async def add_article_async(url, title, summary, image_url, source_feed) -> int | None:
     """Adds a new article record and returns its ID, or None on failure."""
     return await asyncio.to_thread(add_article, url, title, summary, image_url, source_feed)

def add_article(url, title, summary, image_url, source_feed) -> int | None:
    """Adds a new article record and returns its ID, or None on failure."""
    article_id = None
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO articles (url, title, summary, image_url, source_feed)
                VALUES (?, ?, ?, ?, ?)
            """, (url, title, summary, image_url, source_feed))
            article_id = cursor.lastrowid # Get the ID of the inserted row
            conn.commit()
        if article_id:
            log_title = title[:60] + '...' if len(title) > 60 else title
            logger.info(f"Article added to DB (ID: {article_id}): '{log_title}' ({url})")
        return article_id
    except sqlite3.IntegrityError:
        # This likely means the URL was already added (UNIQUE constraint) - expected race condition possibility
        logger.warning(f"Attempted to add duplicate article URL to DB (IntegrityError): {url}")
        # Optionally, retrieve the existing ID if needed, but returning None is simpler
        return None
    except sqlite3.Error as e:
        logger.error(f"Database error adding article {url}: {e}", exc_info=True)
        return None # Indicate failure

async def get_recent_articles_async(limit=MAX_RECENT_ARTICLES_FOR_CHECK):
    """Retrieves recent article titles and URLs for deduplication asynchronously."""
    return await asyncio.to_thread(get_recent_articles, limit)

def get_recent_articles(limit=MAX_RECENT_ARTICLES_FOR_CHECK):
    """Retrieves recent article titles and URLs for deduplication (blocking)."""
    articles = []
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT title, url FROM articles ORDER BY timestamp DESC LIMIT ?", (limit,))
            articles = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Database error retrieving recent articles (for dedupe): {e}", exc_info=True)
    return articles

async def get_recent_articles_details_async(limit: int = 5):
    """Retrieves full details of the most recent articles asynchronously."""
    # New function to get full details
    return await asyncio.to_thread(get_recent_articles_details, limit)

def get_recent_articles_details(limit: int = 5):
    """Retrieves full details of the most recent articles (blocking)."""
    # New function to get full details
    articles = []
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            conn.row_factory = sqlite3.Row # Make sure rows can be treated like dicts
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, url, title, summary, image_url, source_feed
                FROM articles
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            articles = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Database error retrieving recent article details: {e}", exc_info=True)
    return articles

async def get_article_details_async(article_id: int):
    """Retrieves full details for a specific article ID asynchronously."""
    return await asyncio.to_thread(get_article_details, article_id)

def get_article_details(article_id: int):
    """Retrieves full details for a specific article ID (blocking)."""
    details = None
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT url, title, summary, image_url, source_feed FROM articles WHERE id = ?", (article_id,))
            row = cursor.fetchone()
            if row:
                details = dict(row)
            else:
                logger.warning(f"No article found in DB for ID: {article_id}")
    except sqlite3.Error as e:
        logger.error(f"Database error retrieving article details for ID {article_id}: {e}", exc_info=True)
    return details

# --- Webhook Function (Now called from Telegram Callback) ---
async def send_to_webhook_async(data):
     """Sends data to the webhook asynchronously."""
     return await asyncio.to_thread(send_to_webhook, data)

def send_to_webhook(data):
    """Sends data as JSON to the configured webhook URL (blocking)."""
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not configured. Cannot send data.")
        return False
    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(WEBHOOK_URL, headers=headers, json=data, timeout=WEBHOOK_TIMEOUT)
        response.raise_for_status()
        logger.info(f"Successfully sent data to webhook: {data.get('title', 'N/A')}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending data to webhook for {data.get('url', 'N/A')}: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred sending data to webhook for {data.get('url', 'N/A')}: {e}", exc_info=True)
        return False

# --- Deduplication Function ---
async def is_duplicate_async(new_article_title, new_article_summary, recent_articles):
     """Runs the AI deduplication check asynchronously."""
     if not openrouter_client or not recent_articles: # Use openrouter_client specifically
         logger.info("Skipping AI deduplication check (no client or no recent articles).")
         return False
     # The OpenAI client might be async depending on version, but let's wrap for safety/consistency
     return await asyncio.to_thread(is_duplicate, new_article_title, new_article_summary, recent_articles)

def is_duplicate(new_article_title, new_article_summary, recent_articles):
    """Uses GPT via OpenRouter to check if the new article is a duplicate (blocking)."""
    # client and recent_articles check moved to async wrapper
    recent_titles = "\n".join([f"- {a['title']}" for a in recent_articles])
    summary_snippet = (new_article_summary or "")[:1500]

    prompt = f"""
    Here is a list of titles of recently published news articles:
    {recent_titles}

    Here is the title and summary of a new article from an RSS feed:
    Title: {new_article_title}
    Summary: {summary_snippet}...

    Is the new article discussing essentially the same core news event as any of the articles in the list of recent titles?
    Focus on the main subject and event, not just similar keywords. For example, two different articles about the same product launch are duplicates. Two articles discussing different aspects of AI regulation might not be duplicates.
    Answer with only "Yes" or "No".
    """

    try:
        # Use the specific client for deduplication
        completion = openrouter_client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant checking for duplicate news articles based on titles and summaries from RSS feeds."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=10,
            temperature=0.1,
        )
        response_text = completion.choices[0].message.content.strip().lower()
        is_dup = response_text.startswith('yes')
        logger.info(f"AI determined duplicate status for '{new_article_title}': {is_dup}")
        return is_dup
    except Exception as e:
        logger.error(f"Error during AI deduplication check for '{new_article_title}': {e}", exc_info=True)
        return False # Assume not duplicate on error

# --- Image Generation & Upload Functions ---

def _generate_image_openai_sync(prompt: str) -> bytes | None:
    """Synchronous helper to generate image using OpenAI API."""
    if not image_client:
        logger.warning("Image generation client not available.")
        return None
    try:
        logger.info(f"Generating image with prompt: {prompt[:100]}...")
        response = image_client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            n=1,
            size=IMAGE_SIZE,
            quality=IMAGE_QUALITY
        )
        b64_data = response.data[0].b64_json
        if not b64_data:
             logger.error("OpenAI response did not contain b64_json data.")
             return None
        image_bytes = base64.b64decode(b64_data)
        logger.info(f"Image generated successfully ({len(image_bytes)} bytes).")
        return image_bytes
    except Exception as e:
        logger.error(f"Error generating image with OpenAI: {e}", exc_info=True)
        return None

async def generate_image_async(prompt: str) -> bytes | None:
    """Generates an image using OpenAI API asynchronously."""
    return await asyncio.to_thread(_generate_image_openai_sync, prompt)

def _upload_to_catbox_sync(image_bytes: bytes, filename: str = "image.png") -> str | None:
    """Synchronous helper to upload image bytes to Catbox."""
    try:
        files = {'fileToUpload': (filename, image_bytes)}
        data = {'reqtype': 'fileupload', 'userhash': CATBOX_USER_HASH}
        logger.info(f"Uploading {len(image_bytes)} bytes to Catbox...")
        response = requests.post(CATBOX_API_URL, files=files, data=data, timeout=60) # Increased timeout for upload
        response.raise_for_status() # Check for HTTP errors
        image_url = response.text
        # Basic validation for URL
        if image_url.startswith("http://") or image_url.startswith("https://"):
            logger.info(f"Image uploaded successfully to Catbox: {image_url}")
            return image_url
        else:
            logger.error(f"Catbox returned an unexpected response: {response.text}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error uploading image to Catbox: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error during Catbox upload: {e}", exc_info=True)
        return None

async def upload_to_catbox_async(image_bytes: bytes, filename: str = "image.png") -> str | None:
    """Uploads image bytes to Catbox asynchronously."""
    if not image_bytes:
        return None
    return await asyncio.to_thread(_upload_to_catbox_sync, image_bytes, filename)

async def generate_and_upload_image_async(title: str, summary: str | None) -> str | None:
    """Generates an image based on title/summary and uploads it to Catbox."""
    if not IMAGE_GENERATION_ENABLED or not image_client:
        logger.info("Image generation is disabled or client not configured. Skipping.")
        return None

    # Create a concise prompt for the image model
    summary_snippet = (summary or "")[:500] # Limit summary length for prompt
    # Refined prompt: Removed explicit size string, focused on style.
    prompt = (
        f"Generate an engaging Instagram-style thumbnail representing the following news article. "
        f"Focus on the core subject visually. Avoid text, or use at most 1-2 relevant words clearly. "
        f"News Title: {title}. Summary: {summary_snippet}..."
    )

    image_bytes = await generate_image_async(prompt)

    if not image_bytes:
        logger.warning(f"Image generation failed for title: {title}. No image bytes received.")
        return None
    else:
        logger.info(f"Image generated successfully for title: {title}. Proceeding to upload.")


    # Create a somewhat unique filename (optional, Catbox handles uniqueness)
    safe_title = "".join(c if c.isalnum() else '_' for c in title[:30])
    filename = f"{safe_title}_{int(time.time())}.png"

    catbox_url = await upload_to_catbox_async(image_bytes, filename)

    if not catbox_url:
        logger.warning(f"Catbox upload failed for title: {title}.")
        return None
    else:
        logger.info(f"Image uploaded to Catbox for title: {title} -> {catbox_url}")
        return catbox_url

# --- Telegram Interaction Functions ---
async def send_to_telegram_group(context: ContextTypes.DEFAULT_TYPE, article_id: int, title: str, url: str, summary: str, image_url: str | None, source_feed: str):
    """Formats and sends a message with image/button to Telegram group, using article_id for callback."""
    bot = context.bot
    summary_preview = summary[:SUMMARY_PREVIEW_LIMIT] + ('...' if len(summary) > SUMMARY_PREVIEW_LIMIT else '')
    caption = f"📰 *{title}*\n\n{summary_preview}\n\n🔗 {url}\n\nFeed: _{source_feed}_"

    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        cutoff = len(caption) - TELEGRAM_CAPTION_LIMIT - 3
        summary_preview = summary[:SUMMARY_PREVIEW_LIMIT - cutoff] + '...'
        caption = f"📰 *{title}*\n\n{summary_preview}\n\n🔗 {url}\n\nFeed: _{source_feed}_"

    # Prepare button using article_id (as string) for callback_data
    callback_data_str = str(article_id)
    if len(callback_data_str.encode('utf-8')) > 64:
        logger.error(f"Generated article_id string '{callback_data_str}' is too long for callback_data! Skipping button.")
        reply_markup = None # Should not happen with IDs, but safety check
    else:
        keyboard = [[InlineKeyboardButton("⬆️ Subir Noticia", callback_data=callback_data_str)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    message_id = None
    try:
        if image_url:
            logger.debug(f"Attempting to send photo: {image_url} with button data: {callback_data_str}")
            sent_message = await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
            )
            message_id = sent_message.message_id
        else:
            logger.debug(f"No image_url found, sending text message with button data: {callback_data_str}")
            sent_message = await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup, disable_web_page_preview=True
            )
            message_id = sent_message.message_id
        logger.info(f"Message sent to Telegram for article ID {article_id}: {title}")
        return message_id
    except TelegramError as e:
        # Log the specific error if it happens again
        logger.error(f"Telegram API error sending message for article ID {article_id} ('{title}'): {e}", exc_info=True)
        # Fallback logic remains the same
        if image_url and "PHOTO_INVALID" in str(e) or "wrong file identifier" in str(e) or "Failed to get http url content" in str(e):
             logger.warning(f"Failed to send photo for {title}. Trying text message instead.")
             try:
                 # Need to resend caption and button data
                 sent_message = await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup, disable_web_page_preview=True
                 )
                 message_id = sent_message.message_id
                 return message_id
             except TelegramError as e2:
                  logger.error(f"Telegram API error sending fallback text message for article ID {article_id} ('{title}'): {e2}", exc_info=True)
        return None # Indicate failure
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram message for article ID {article_id} ('{title}'): {e}", exc_info=True)
        return None

async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Upload News' button press, retrieving article by ID."""
    callback_query = update.callback_query
    await callback_query.answer("Procesando...")

    # Get article_id (string) from data and convert to int
    try:
        article_id_str = callback_query.data
        article_id = int(article_id_str)
        logger.info(f"Button pressed for article ID: {article_id}")
    except (ValueError, TypeError):
        logger.error(f"Invalid callback_data received: '{callback_query.data}'. Expected an integer article ID.")
        try:
            await callback_query.edit_message_text(text="❌ Error: Datos del botón inválidos.")
        except TelegramError as e:
            logger.error(f"Error editing message after invalid button data: {e}")
        return

    # Retrieve full article details from DB using the ID
    article_details = await get_article_details_async(article_id)

    if not article_details:
        logger.error(f"Could not retrieve article details from DB for ID: {article_id}")
        try:
            # Check if message exists before trying to edit
            if callback_query.message:
                 # Check if message has text or caption before editing appropriate field
                 if callback_query.message.text:
                      await callback_query.edit_message_text(text="❌ Error: No se encontraron los detalles del artículo (ID: {}).".format(article_id))
                 elif callback_query.message.caption:
                      await callback_query.edit_message_caption(caption="❌ Error: No se encontraron los detalles del artículo (ID: {}).".format(article_id))
                 else: # Fallback just in case? Unlikely.
                      logger.warning(f"Message {callback_query.message.message_id} for article ID {article_id} has neither text nor caption to edit.")
            else:
                 logger.warning(f"Callback query {callback_query.id} for article ID {article_id} has no associated message to edit.")
        except TelegramError as e:
             logger.error(f"Error editing message after DB details failure for ID {article_id}: {e}")
        return

    if not WEBHOOK_URL:
         # Logic remains the same
         logger.warning(f"Webhook URL not set. Cannot process button press for article ID {article_id}")
         try:
            await callback_query.edit_message_text(text="⚠️ Webhook no configurado. No se puede subir.")
         except TelegramError as e:
             logger.error(f"Error editing message for missing webhook: {e}")
         return

    # Prepare data for webhook
    webhook_data = {
        'title': article_details['title'],
        'url': article_details['url'], # Still send the URL to the webhook
        'content_preview': (article_details['summary'] or "")[:1000] + ('...' if len(article_details['summary'] or "") > 1000 else ''),
        'image_url': article_details['image_url'],
        'source': article_details['source_feed']
    }

    # Send data to webhook
    success = await send_to_webhook_async(webhook_data)

    # Edit the original Telegram message
    # Logic remains largely the same, just using article_id in logs
    try:
        original_message = callback_query.message
        new_text = original_message.text or original_message.caption # Get original text/caption
        if success:
            status_text = "\n\n✅ *Noticia enviada al webhook.*"
            new_reply_markup = None # Remove button
        else:
            status_text = "\n\n❌ *Error al enviar al webhook.*"
            new_reply_markup = None # Remove button

        final_text = (new_text or "") + status_text

        if original_message.photo:
             # Ensure final caption is within limits
            if len(final_text) > TELEGRAM_CAPTION_LIMIT:
                diff = len(final_text) - TELEGRAM_CAPTION_LIMIT
                original_trimmed = (new_text or "")[:-(diff + 3)] + "..."
                final_text = original_trimmed + status_text

            await callback_query.edit_message_caption(
                caption=final_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=new_reply_markup
            )
        else:
            if len(final_text) > 4096:
                 final_text = final_text[:4093] + "..."
            await callback_query.edit_message_text(
                text=final_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=new_reply_markup,
                disable_web_page_preview=True
            )
        logger.info(f"Edited Telegram message for article ID {article_id}. Webhook success: {success}")

    except TelegramError as e:
        if "message is not modified" in str(e):
             logger.warning(f"Message for article ID {article_id} was already edited or not modified.")
        else:
             logger.error(f"Failed to edit Telegram message for article ID {article_id}: {e}", exc_info=True)
    except Exception as e:
         logger.error(f"Unexpected error editing Telegram message for article ID {article_id}: {e}", exc_info=True)

# --- Test Command Function ---
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fetches the latest article from a random feed source, attempts to generate
    an image, and sends it to the Telegram group as a test message without
    saving to the DB or checking duplicates.
    """
    user = update.effective_user
    logger.info(f"Received /test command from {user.username or user.first_name} (ID: {user.id})")

    if not NEWS_URLS:
        await update.message.reply_text("⚠️ No hay fuentes de noticias (NEWS_URLS) configuradas.")
        return

    await update.message.reply_text("⚙️ Intentando obtener y enviar una noticia de prueba desde una fuente aleatoria...")

    # 1. Select a random feed URL
    feed_url = random.choice(NEWS_URLS)
    logger.info(f"[/test] Selected random feed: {feed_url}")

    article_to_send = None
    try:
        # 2. Fetch the feed
        feed_data = await asyncio.to_thread(
            feedparser.parse, feed_url,
            request_headers={'User-Agent': 'NewsBot/1.0 (+http://yourdomain.com/newsbotinfo)'}
        )

        if feed_data.bozo:
            logger.warning(f"[/test] Feed potentially malformed: {feed_url}. Reason: {feed_data.bozo_exception}")
        if not feed_data.entries:
            logger.warning(f"[/test] No entries found in feed: {feed_url}")
            await update.message.reply_text(f"⚠️ No se encontraron artículos en la fuente seleccionada ({feed_url}). Intenta de nuevo.")
            return

        # 3. Select the first valid entry
        for entry in feed_data.entries:
            article_url = getattr(entry, 'link', None)
            article_title = getattr(entry, 'title', None)
            if not article_url or not article_title: continue

            article_url = urljoin(feed_url, article_url) # Ensure URL is absolute

            logger.info(f"[/test] Found article candidate: '{article_title}' ({article_url})")

            # 4. Extract data
            summary = getattr(entry, 'summary', getattr(entry, 'description', None))
            original_image_url = None
            if 'media_content' in entry and entry.media_content:
                original_image_url = entry.media_content[0].get('url')
            elif 'links' in entry:
                for link in entry.links:
                    if link.get('rel') == 'enclosure' and link.get('type','').startswith('image/'):
                        original_image_url = link.get('href'); break

            # 5. Clean summary
            cleaned_summary = summary
            if "reddit.com" in feed_url and cleaned_summary:
                try:
                    soup = BeautifulSoup(cleaned_summary, 'html.parser')
                    cleaned_summary = soup.get_text(separator=' ', strip=True)
                    logger.debug(f"[/test] Cleaned HTML tags from Reddit summary.")
                except Exception as e:
                    logger.warning(f"[/test] Could not clean HTML tags from summary: {e}")
            if cleaned_summary:
                try:
                    cleaned_summary = html.unescape(cleaned_summary)
                    logger.debug(f"[/test] Unescaped HTML entities.")
                except Exception as e:
                    logger.warning(f"[/test] Could not unescape HTML entities: {e}")

            # Store details and break loop (we only want the first one)
            article_to_send = {
                'url': article_url,
                'title': article_title,
                'summary': cleaned_summary or "",
                'original_image_url': original_image_url,
                'source_feed': feed_url
            }
            break # Found the first valid article

        if not article_to_send:
            logger.warning(f"[/test] No valid articles with title and link found in feed: {feed_url}")
            await update.message.reply_text(f"⚠️ No se encontraron artículos válidos en la fuente seleccionada ({feed_url}). Intenta de nuevo.")
            return

    except Exception as e:
        logger.error(f"[/test] Failed to process feed URL {feed_url}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error al procesar la fuente: {feed_url}")
        return

    # 6. Attempt Image Generation (Bypassing DB/Dedupe checks)
    logger.info(f"[/test] Attempting image generation for: {article_to_send['title']}")
    generated_image_url = await generate_and_upload_image_async(
        article_to_send['title'],
        article_to_send['summary']
    )

    # 7. Determine final image URL
    final_image_url = generated_image_url if generated_image_url else article_to_send['original_image_url']
    logger.info(f"[/test] Final image URL selected: {final_image_url}")

    # 8. Send the single test article to Telegram
    # Using article_id=0 as it's not stored and callback is irrelevant for test
    test_title = f"[TEST] {article_to_send['title']}"
    message_id = await send_to_telegram_group(
        context,
        article_id=0, # Dummy ID for test
        title=test_title,
        url=article_to_send['url'],
        summary=article_to_send['summary'],
        image_url=final_image_url,
        source_feed=article_to_send['source_feed']
    )

    # 9. Report result
    if message_id:
        result_message = f"✅ Mensaje de prueba enviado al grupo desde {feed_url}."
        logger.info(f"Test command finished successfully for {user.username or user.first_name}.")
    else:
        result_message = f"❌ Falló el envío del mensaje de prueba desde {feed_url} al grupo."
        logger.error(f"Test command failed for {user.username or user.first_name}.")

    await update.message.reply_text(result_message)

# --- Function to Populate DB Initially (Synchronous) ---
def populate_initial_db():
    """
    Reads feeds on startup and adds articles to DB without sending notifications
    or generating new images. Uses feed image URL if available.
    """
    logger.info("Starting initial database population...")
    processed_urls_population = set()

    for feed_url in NEWS_URLS:
        logger.info(f"[Population] Checking feed: {feed_url}")
        try:
            socket.setdefaulttimeout(FEED_REQUEST_TIMEOUT)
            feed_data = feedparser.parse(feed_url)
            socket.setdefaulttimeout(None)

            if feed_data.bozo:
                logger.warning(f"[Population] Feed potentially malformed: {feed_url}. Reason: {feed_data.bozo_exception}")
            if not feed_data.entries:
                continue

            added_count = 0
            skipped_count = 0
            for entry in feed_data.entries:
                article_url = getattr(entry, 'link', None)
                article_title = getattr(entry, 'title', None)
                if not article_url or not article_title: continue

                article_url = urljoin(feed_url, article_url)
                if article_url in processed_urls_population: continue
                processed_urls_population.add(article_url)

                # Only add if it doesn't exist. Fetch summary/image for DB now.
                if not article_exists(article_url):
                    summary = getattr(entry, 'summary', getattr(entry, 'description', None))
                    # Clean HTML from summary if it's from Reddit
                    if "reddit.com" in feed_url and summary:
                        try:
                            soup = BeautifulSoup(summary, 'html.parser')
                            summary = soup.get_text(separator=' ', strip=True)
                            logger.debug(f"[Population] Cleaned HTML tags from Reddit summary for: {article_url}")
                        except Exception as e:
                            logger.warning(f"[Population] Could not clean HTML tags from summary for {article_url}: {e}")
                    # Unescape HTML entities for all summaries
                    if summary:
                        try:
                            summary = html.unescape(summary)
                            logger.debug(f"[Population] Unescaped HTML entities for: {article_url}")
                        except Exception as e:
                            logger.warning(f"[Population] Could not unescape HTML entities for {article_url}: {e}")

                    # Extract image URL from feed ONLY for population
                    feed_image_url = None
                    if 'media_content' in entry and entry.media_content:
                        feed_image_url = entry.media_content[0].get('url')
                    elif 'links' in entry:
                        for link in entry.links:
                            if link.get('rel') == 'enclosure' and link.get('type','').startswith('image/'):
                                feed_image_url = link.get('href')
                                break

                    # Add article with the feed image URL (or None)
                    add_article(article_url, article_title, summary, feed_image_url, feed_url)
                    added_count += 1
                else:
                    skipped_count += 1
            logger.info(f"[Population] Finished feed {feed_url}. Added: {added_count}, Skipped: {skipped_count}")

        except socket.timeout:
            logger.error(f"[Population] Timeout error fetching feed: {feed_url}")
        except Exception as e:
            logger.error(f"[Population] Failed to process feed URL {feed_url}: {e}", exc_info=True)
        time.sleep(0.5) # Shorter delay during population

    logger.info("Initial database population finished.")
    socket.setdefaulttimeout(None) # Reset just in case

# --- Main Job Function (Now Async for PTB Job Queue) ---
async def check_news(context: ContextTypes.DEFAULT_TYPE):
    """The main async job function executed by the Job Queue."""
    logger.info("Starting news check cycle...")
    recent_db_articles = await get_recent_articles_async()

    for feed_url in NEWS_URLS:
        logger.info(f"Checking feed: {feed_url}")
        new_articles_found_feed = 0
        processed_urls_this_cycle = set() # Track URLs processed within this feed fetch

        try:
            # Fetch and parse feed data
            feed_data = await asyncio.to_thread(
                feedparser.parse, feed_url,
                request_headers={'User-Agent': 'NewsBot/1.0 (+http://yourdomain.com/newsbotinfo)'}
            )
            # ... (bozo check, no entries check remain the same) ...
            if feed_data.bozo:
                 logger.warning(f"Feed potentially malformed: {feed_url}. Reason: {feed_data.bozo_exception}")
            if not feed_data.entries:
                 logger.info(f"No entries found in feed: {feed_url}")
                 continue # Correct indentation for continue


            logger.info(f"Found {len(feed_data.entries)} entries in feed: {feed_url}")

            for entry in feed_data.entries:
                article_url = getattr(entry, 'link', None)
                article_title = getattr(entry, 'title', None)
                if not article_url or not article_title: continue

                article_url = urljoin(feed_url, article_url)
                if article_url in processed_urls_this_cycle: continue
                processed_urls_this_cycle.add(article_url)

                # 1. Check DB for URL existence (Async)
                if await article_exists_async(article_url):
                    logger.debug(f"Article already in DB: {article_url}")
                    continue

                logger.info(f"Found potential new article: '{article_title}' ({article_url})")

                # --- Extract summary and potentially original image ---
                summary = getattr(entry, 'summary', getattr(entry, 'description', None))
                original_image_url = None # Extract original image URL as fallback
                if 'media_content' in entry and entry.media_content:
                    original_image_url = entry.media_content[0].get('url')
                elif 'links' in entry:
                    for link in entry.links:
                         if link.get('rel') == 'enclosure' and link.get('type','').startswith('image/'):
                            original_image_url = link.get('href'); break

                # Clean HTML from summary (Reddit specific and general unescape)
                cleaned_summary = summary # Start with original summary
                if "reddit.com" in feed_url and cleaned_summary:
                    try:
                        soup = BeautifulSoup(cleaned_summary, 'html.parser')
                        cleaned_summary = soup.get_text(separator=' ', strip=True)
                        logger.debug(f"Cleaned HTML tags from Reddit summary for: {article_url}")
                    except Exception as e:
                        logger.warning(f"Could not clean HTML tags from summary for {article_url}: {e}")
                if cleaned_summary:
                    try:
                        cleaned_summary = html.unescape(cleaned_summary)
                        logger.debug(f"Unescaped HTML entities for: {article_url}")
                    except Exception as e:
                        logger.warning(f"Could not unescape HTML entities for {article_url}: {e}")


                # 2. AI Deduplication Check (Async)
                is_ai_duplicate = await is_duplicate_async(article_title, cleaned_summary, recent_db_articles)
                if is_ai_duplicate:
                     logger.info(f"Article determined as duplicate by AI, skipping: {article_title}")
                     continue

                # --- Generate and Upload Image ---
                generated_image_url = await generate_and_upload_image_async(article_title, cleaned_summary)

                # Determine final image URL (prefer generated, fallback to original feed image)
                final_image_url = generated_image_url if generated_image_url else original_image_url

                # 3. Add to DB first to get ID (Async) - Store the final determined image URL
                article_id = await add_article_async(article_url, article_title, cleaned_summary, final_image_url, feed_url)

                if not article_id:
                     logger.warning(f"Failed to add article to DB or article already exists (race condition?), skipping Telegram send for: {article_url}")
                     continue # Skip if we couldn't add it / get an ID

                # 4. Send to Telegram Group with the obtained ID and final image URL (Async)
                message_id = await send_to_telegram_group(
                    context, article_id, article_title, article_url, cleaned_summary or "", final_image_url, feed_url
                )

                if message_id:
                    # Article already added, just update counts and recent list
                    new_articles_found_feed += 1
                    recent_db_articles.insert(0, {'title': article_title, 'url': article_url}) # Keep recent list based on URL/title for dedupe
                    if len(recent_db_articles) > MAX_RECENT_ARTICLES_FOR_CHECK: recent_db_articles.pop()
                else:
                    # If sending fails, we ideally should remove the article from the DB or mark it as failed,
                    # otherwise it won't be picked up again. For now, just log the error.
                    # Consider adding a 'sent_to_telegram' flag to the DB later.
                    logger.error(f"Failed to send article ID {article_id} to Telegram: {article_title}")

                await asyncio.sleep(1.5) # Slightly increased delay to accommodate image generation/upload time

            logger.info(f"Finished checking {feed_url}. Processed {new_articles_found_feed} new articles.")

        except Exception as e:
            logger.error(f"Failed to process feed URL {feed_url}: {e}", exc_info=True)

        await asyncio.sleep(2) # Increased delay between feeds

    logger.info(f"News check cycle finished. Next run scheduled in {CHECK_INTERVAL_SECONDS / 60} minutes.")

# --- Main Execution ---
if __name__ == "__main__":
    init_db()
    populate_initial_db()

    logger.info("Setting up Telegram Bot Application...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CallbackQueryHandler(handle_button_press))
    application.add_handler(CommandHandler("test", test_command))

    job_queue = application.job_queue
    if not job_queue:
         logger.error("JobQueue not available. Please install with 'pip install \"python-telegram-bot[job-queue]\"'. Exiting.")
         exit(1)

    job_queue.run_repeating(check_news, interval=CHECK_INTERVAL_SECONDS, first=10)
    logger.info(f"Scheduled news check job to run every {CHECK_INTERVAL_SECONDS} seconds.")

    logger.info("Starting Telegram Bot Polling...")
    application.run_polling()
