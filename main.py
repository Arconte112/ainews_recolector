import requests
from bs4 import BeautifulSoup # Added back for HTML cleaning
import time
import sqlite3
import json
import os
from urllib.parse import urljoin
import logging
from dotenv import load_dotenv
import feedparser # Added feedparser
import socket # Ensure socket is imported globally if used in both functions
import asyncio # Added asyncio
import html # Added for unescaping HTML entities
import sys # Added for sys.executable
import uuid # Added for unique filenames
import base64 # Add back base64 import
from requests.adapters import HTTPAdapter, Retry # Import Retry and HTTPAdapter

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue # Added JobQueue
from telegram.constants import ParseMode
from telegram.error import TelegramError

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
# Reduce telegram logger verbosity
logging.getLogger("telegram.vendor.ptb_urllib3.urllib3.connectionpool").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)
logging.getLogger("telegram.bot").setLevel(logging.INFO) # Keep INFO for bot actions
logger = logging.getLogger(__name__)

# --- Configuration ---
load_dotenv()

# --- Environment Variables & Constants ---
# NEWS_URLS = json.loads(os.getenv('NEWS_URLS', '[]')) # Replaced with hardcoded list
NEWS_URLS = ["https://techcrunch.com/category/artificial-intelligence/feed/", "https://www.wired.com/feed/tag/ai/latest/rss", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "https://arstechnica.com/tag/artificial-intelligence/feed/", "https://feed.infoq.com/ai-ml-data-eng/news", "https://futurism.com/categories/ai-artificial-intelligence/feed", "https://www.theguardian.com/technology/artificialintelligenceai/rss", "https://www.reddit.com/r/artificial/.rss,https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/tecnologia/portada"]
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
if not WEBHOOK_URL:
    logger.error("❌ WEBHOOK_URL no está configurado en el archivo .env. El script no puede funcionar sin él.")
    exit(1)
else:
    logger.info(f"WEBHOOK_URL configurado como: {WEBHOOK_URL}")

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no están configurados en .env. Se requiere para enviar noticias a Telegram.")
    exit(1)
else:
    logger.info(f"Telegram Bot Token y Chat ID configurados. Enviando a: {TELEGRAM_CHAT_ID}")

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_IMAGE_MODEL = os.getenv('OPENAI_IMAGE_MODEL', "gpt-image-1") # Changed default model
IMAGE_GENERATION_ENABLED = os.getenv('IMAGE_GENERATION_ENABLED', 'True').lower() == 'true'
IMAGE_SIZE = os.getenv('IMAGE_SIZE', '1024x1024')
IMAGE_QUALITY = os.getenv('IMAGE_QUALITY', 'medium') # Changed default quality

# CATBOX_API_URL = "https://catbox.moe/user/api.php" # Removed Catbox URL
IMGBB_API_URL = "https://api.imgbb.com/1/upload"
IMGBB_API_KEY = "fa96877128b39591c1286e74e07e0987" # Your ImgBB API Key

GPT_MODEL = os.getenv('GPT_MODEL', "openai/gpt-4o-mini")
DATABASE_NAME = 'news_log.db'
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_MINUTES', 15)) * 60
MAX_RECENT_ARTICLES_FOR_CHECK = int(os.getenv('MAX_RECENT_ARTICLES_FOR_CHECK', 30))
FEED_REQUEST_TIMEOUT = int(os.getenv('FEED_REQUEST_TIMEOUT', 30))
WEBHOOK_TIMEOUT = int(os.getenv('WEBHOOK_TIMEOUT', 45))
TELEGRAM_TIMEOUT = int(os.getenv('TELEGRAM_TIMEOUT', 30)) # Added timeout for Telegram

# Define a directory for temporary images
TEMP_IMAGE_DIR = "temp_images"

# --- OpenAI Client Setup ---
openrouter_client = None
if OPENROUTER_API_KEY:
    from openai import OpenAI
    openrouter_client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
    )
    logger.info("OpenRouter client configured for deduplication.")
else:
    logger.warning("OPENROUTER_API_KEY not found. Deduplication feature will be disabled.")

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
                    summary TEXT,
                    image_url TEXT,
                    source_feed TEXT,
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
            log_title_short = title[:50] + '...' if len(title) > 50 else title
            logger.info(f"Artículo añadido a BD (ID: {article_id}): '{log_title_short}'")
        return article_id
    except sqlite3.IntegrityError:
        logger.debug(f"Intento de añadir URL duplicada a BD (Ignorado): {url}")
        return None
    except sqlite3.Error as e:
        logger.error(f"Database error adding article {url}: {e}", exc_info=True)
        return None

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

# --- Webhook Function ---
async def send_to_webhook_async(data):
     """Sends data to the webhook asynchronously."""
     if not WEBHOOK_URL:
         logger.error("WEBHOOK_URL not configured. Cannot send data.")
         return False
     try:
         headers = {'Content-Type': 'application/json'}
         logger.debug(f"Enviando datos a webhook URL: {WEBHOOK_URL}")
         logger.debug(f"Contenido de la petición: {json.dumps(data)}")

         response = await asyncio.to_thread(
             requests.post, WEBHOOK_URL, headers=headers, json=data, timeout=WEBHOOK_TIMEOUT
         )

         logger.debug(f"Respuesta del webhook: Status={response.status_code}, Content={response.text[:500]}")

         response.raise_for_status()
         log_title = data.get('title', 'N/A')
         log_title_short = log_title[:60] + '...' if len(log_title) > 60 else log_title
         logger.info(f"Artículo enviado correctamente al webhook: '{log_title_short}'")
         return True
     except requests.exceptions.Timeout:
         log_title = data.get('title', 'N/A')
         logger.error(f"Timeout al enviar datos al webhook para '{log_title}'. URL: {WEBHOOK_URL}")
         return False
     except requests.exceptions.RequestException as e:
         log_title = data.get('title', 'N/A')
         status_code = e.response.status_code if e.response is not None else "N/A"
         logger.error(f"Error ({status_code}) al enviar datos al webhook para '{log_title}': {e}")
         return False
     except Exception as e:
         log_title = data.get('title', 'N/A')
         logger.error(f"Error inesperado al enviar datos al webhook para '{log_title}': {e}", exc_info=True)
         return False

# --- Deduplication Function ---
async def is_duplicate_async(new_article_title, new_article_summary, recent_articles):
     """Runs the AI deduplication check asynchronously."""
     if not openrouter_client or not recent_articles:
         logger.debug("Skipping AI deduplication check (no client or no recent articles).")
         return False
     return await asyncio.to_thread(is_duplicate, new_article_title, new_article_summary, recent_articles)

def is_duplicate(new_article_title, new_article_summary, recent_articles):
    """Uses GPT via OpenRouter to check if the new article is a duplicate (blocking)."""
    if not openrouter_client:
        return False
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
        logger.debug(f"AI determined duplicate status for '{new_article_title}': {is_dup}")
        return is_dup
    except Exception as e:
        logger.error(f"Error during AI deduplication check for '{new_article_title}': {e}", exc_info=True)
        return False

# --- ImgBB Upload Functions --- (Renamed from Catbox)

# Session creation can remain similar, retry logic is generally good
def _create_upload_session() -> requests.Session:
    """Creates a requests Session with retry logic for uploads."""
    session = requests.Session()
    retries = Retry(total=3,
                    backoff_factor=0.3,
                    status_forcelist=[500, 502, 503, 504],
                    allowed_methods=None,
                    respect_retry_after_header=True)
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

def _upload_to_imgbb_sync(session: requests.Session, image_base64: str, filename: str | None = None) -> str | None:
    """
    Synchronous helper to upload a base64 image string to ImgBB anonymously
    using a session with retries. Parses JSON response for URL.
    """
    try:
        # ImgBB expects base64 string in the 'image' field of the payload
        payload = {
            'key': IMGBB_API_KEY,
            'image': image_base64,
        }
        # Optional: Add filename if provided
        # if filename:
        #     payload['name'] = filename

        # Optional: Add expiration if desired (e.g., 1 day = 86400 seconds)
        # payload['expiration'] = "86400"

        logger.debug(f"Uploading base64 image ({len(image_base64)} chars) to ImgBB...")
        response = session.post(IMGBB_API_URL, data=payload, timeout=120)
        response.raise_for_status() # Raise exception for bad status codes (4xx or 5xx)

        # Parse the JSON response
        try:
            json_response = response.json()
            if json_response.get("success") and json_response.get("data") and json_response["data"].get("url"):
                image_url = json_response["data"]["url"]
                display_url = json_response["data"].get("display_url", image_url) # Prefer display_url if available
                logger.info(f"Image uploaded successfully to ImgBB: {display_url}")
                return display_url # Return the direct image URL or display URL
            else:
                error_message = json_response.get("error", {}).get("message", "Unknown ImgBB API error")
                status_code = json_response.get("status_code", "N/A")
                logger.error(f"ImgBB API error (Status: {status_code}): {error_message}")
                logger.debug(f"Full ImgBB error response: {json_response}")
                return None
        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON response from ImgBB: {response.text[:500]}...")
            return None

    except requests.exceptions.RetryError as e:
         logger.error(f"ImgBB upload failed after multiple retries: {e}", exc_info=True)
         return None
    except requests.exceptions.Timeout:
        logger.error(f"Timeout error during an ImgBB upload attempt.")
        return None
    except requests.exceptions.RequestException as e:
        status = e.response.status_code if e.response is not None else "N/A"
        response_text = e.response.text[:500] if e.response is not None else "N/A"
        logger.error(f"Error ({status}) uploading image to ImgBB: {e}. Response: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error during ImgBB upload: {e}", exc_info=True)
        return None

async def upload_to_imgbb_async(image_base64: str, filename: str | None = None) -> str | None:
    """Uploads a base64 image string to ImgBB asynchronously using a session with retries."""
    if not image_base64:
        logger.warning("No image base64 provided to upload_to_imgbb_async.")
        return None
    session = _create_upload_session()
    # Pass base64 string directly
    result = await asyncio.to_thread(_upload_to_imgbb_sync, session, image_base64, filename)
    session.close()
    return result

# --- Modified Image Generation via Script --- (Adjusted to call ImgBB upload)
async def generate_and_upload_image_via_script_async(prompt: str) -> str | None:
    """
    Calls test2.py script to generate an image, gets base64, uploads to ImgBB,
    and returns the ImgBB URL on success. Returns None on failure.
    """
    if not IMAGE_GENERATION_ENABLED:
        logger.debug("Image generation is disabled. Skipping.")
        return None

    command = [
        sys.executable,
        'test2.py',
        '--prompt', prompt
    ]

    logger.info(f"Executing image generation script: test2.py")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()
    stdout_decoded = stdout.decode().strip() if stdout else ""
    stderr_decoded = stderr.decode().strip() if stderr else ""

    if process.returncode != 0 or not stdout_decoded:
        logger.error(f"Image generation script failed (Code: {process.returncode}) or produced no stdout.")
        if stderr_decoded: logger.error(f"Script stderr: {stderr_decoded}")
        return None

    image_base64 = stdout_decoded
    logger.info(f"Image generation script succeeded. Received base64 data ({len(image_base64)} chars).")

    # No decoding needed here, pass base64 directly to ImgBB upload function
    # try: ... image_bytes = base64.b64decode(image_base64) ... except # Removed

    # Upload the base64 string to ImgBB
    imgbb_url = await upload_to_imgbb_async(image_base64) # Pass base64 string

    if imgbb_url:
        logger.info(f"Successfully generated and uploaded image to ImgBB: {imgbb_url}")
        return imgbb_url
    else:
        logger.error("Failed to upload generated image to ImgBB.")
        return None

# --- Function to Populate DB Initially (Synchronous) ---
def populate_initial_db():
    """
    Reads feeds on startup and adds articles to DB without sending notifications
    or generating new images. Uses feed image URL if available.
    """
    logger.info("Starting initial database population...")
    processed_urls_population = set()

    for feed_url in NEWS_URLS:
        logger.debug(f"[Population] Checking feed: {feed_url}")
        try:
            socket.setdefaulttimeout(FEED_REQUEST_TIMEOUT)
            feed_data = feedparser.parse(feed_url)
            socket.setdefaulttimeout(None)

            if feed_data.bozo:
                logger.warning(f"[Population] Feed potentially malformed: {feed_url}. Reason: {feed_data.bozo_exception}")
            if not feed_data.entries:
                logger.debug(f"[Population] No entries found in feed: {feed_url}")
                continue

            added_count = 0
            skipped_count = 0
            logger.debug(f"[Population] Processing {len(feed_data.entries)} entries for {feed_url}")
            for entry in feed_data.entries:
                article_url = getattr(entry, 'link', None)
                article_title = getattr(entry, 'title', None)
                if not article_url or not article_title: continue

                article_url = urljoin(feed_url, article_url)
                if article_url in processed_urls_population: continue
                processed_urls_population.add(article_url)

                if not article_exists(article_url):
                    summary = getattr(entry, 'summary', getattr(entry, 'description', None))
                    if "reddit.com" in feed_url and summary:
                        try:
                            soup = BeautifulSoup(summary, 'html.parser')
                            summary = soup.get_text(separator=' ', strip=True)
                            logger.debug(f"[Population] Cleaned HTML tags from Reddit summary for: {article_url}")
                        except Exception as e:
                            logger.warning(f"[Population] Could not clean HTML tags from summary for {article_url}: {e}")
                    if summary:
                        try:
                            summary = html.unescape(summary)
                            logger.debug(f"[Population] Unescaped HTML entities for: {article_url}")
                        except Exception as e:
                            logger.warning(f"[Population] Could not unescape HTML entities for {article_url}: {e}")

                    feed_image_url = None
                    if 'media_content' in entry and entry.media_content:
                        feed_image_url = entry.media_content[0].get('url')
                    elif 'links' in entry:
                        for link in entry.links:
                            if link.get('rel') == 'enclosure' and link.get('type','').startswith('image/'):
                                feed_image_url = link.get('href')
                                break

                    if add_article(article_url, article_title, summary, feed_image_url, feed_url):
                        added_count += 1
                else:
                    skipped_count += 1
            logger.info(f"[Population] Finished feed {feed_url}. Added: {added_count}, Skipped (already existed): {skipped_count}")

        except socket.timeout:
            logger.error(f"[Population] Timeout error fetching feed: {feed_url}")
        except Exception as e:
            logger.error(f"[Population] Failed to process feed URL {feed_url}: {e}", exc_info=True)
        time.sleep(0.5)

    logger.info("Initial database population finished.")
    socket.setdefaulttimeout(None)

# --- Telegram Functions ---
async def send_to_telegram_chat(
    bot: ContextTypes.DEFAULT_TYPE.bot,
    chat_id: str,
    article_id: int,
    title: str,
    url: str,
    summary: str | None,
    image_url: str | None # Should always be a URL or None now
):
    """Sends the article details to Telegram, expects image_url to be a URL."""
    try:
        text = f"📰 <b>{html.escape(title)}</b>\n\n"
        if summary:
            max_summary_len = 500
            summary_snippet = summary[:max_summary_len] + ('...' if len(summary) > max_summary_len else '')
            text += f"{html.escape(summary_snippet)}\n\n"
        text += f'<a href="{url}">Leer más</a>'

        keyboard = [[InlineKeyboardButton("⬆️ Subir Noticia", callback_data=str(article_id))]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        log_title_short = title[:50] + '...' if len(title) > 50 else title

        if image_url and image_url.startswith("http"): # Check if it's a URL
            # Handle URL image
            logger.info(f"Enviando a Telegram (con imagen URL: {image_url}) para ID {article_id}: '{log_title_short}'")
            await bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                read_timeout=TELEGRAM_TIMEOUT,
                write_timeout=TELEGRAM_TIMEOUT,
                connect_timeout=TELEGRAM_TIMEOUT
            )
        else:
            if image_url: # Log if a non-URL was somehow passed
                logger.warning(f"Invalid image_url format received: '{image_url}'. Sending without image for ID {article_id}.")
            # No image_url provided or it was invalid
            logger.info(f"Enviando a Telegram (sin imagen) para ID {article_id}: '{log_title_short}'")
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup, disable_web_page_preview=True,
                read_timeout=TELEGRAM_TIMEOUT, write_timeout=TELEGRAM_TIMEOUT,
                connect_timeout=TELEGRAM_TIMEOUT
            )
        return True
    except TelegramError as e:
        # Log specific network errors differently
        if isinstance(e, telegram.error.NetworkError):
             logger.error(f"Error de RED al enviar mensaje a Telegram para artículo ID {article_id}. Imagen URL: {image_url}. Error: {e}", exc_info=True)
        else:
             logger.error(f"Error general de Telegram al enviar mensaje para artículo ID {article_id}. Imagen URL: {image_url}. Error: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Error inesperado al enviar mensaje a Telegram para artículo ID {article_id}. Imagen URL: {image_url}. Error: {e}", exc_info=True)
        return False

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses on Telegram messages."""
    query = update.callback_query
    await query.answer("Procesando...") # Acknowledge button press

    try:
        article_id = int(query.data)
        logger.info(f"Botón 'Subir Noticia' presionado para artículo ID: {article_id}")
    except (ValueError, TypeError):
        logger.error(f"Error: callback_data inválido recibido: {query.data}")
        await query.edit_message_text(text=f"{query.message.caption or query.message.text}\n\n⚠️ Error: ID de artículo inválido.", parse_mode=ParseMode.HTML)
        return

    article_details = await get_article_details_async(article_id)

    if not article_details:
        logger.error(f"No se encontraron detalles en la BD para el artículo ID: {article_id}")
        await query.edit_message_text(
            text=f"{query.message.caption or query.message.text}\n\n⚠️ Error: No se encontró el artículo en la base de datos.",
            parse_mode=ParseMode.HTML # Keep original formatting if possible
        )
        return

    webhook_data = {
        'title': article_details['title'],
        'url': article_details['url'],
        'summary': article_details['summary'] or "",
        'image_url': article_details['image_url'],
        'source_feed': article_details['source_feed'],
    }

    logger.info(f"Enviando artículo ID {article_id} al webhook...")
    success = await send_to_webhook_async(webhook_data)

    if success:
        logger.info(f"Artículo ID {article_id} enviado correctamente al webhook.")
        # Edit message to show success and remove button
        original_text = query.message.caption if query.message.photo else query.message.text
        await query.edit_message_caption(
             caption=f"{original_text}\n\n✅ <b>¡Enviado al webhook!</b>",
             parse_mode=ParseMode.HTML
        ) if query.message.photo else await query.edit_message_text(
             text=f"{original_text}\n\n✅ <b>¡Enviado al webhook!</b>",
             parse_mode=ParseMode.HTML,
             disable_web_page_preview=True # Match original setting
        )
    else:
        logger.error(f"Fallo al enviar artículo ID {article_id} al webhook.")
        await query.edit_message_caption(
            caption=f"{query.message.caption or query.message.text}\n\n❌ <b>Error al enviar al webhook.</b>",
            parse_mode=ParseMode.HTML
        ) if query.message.photo else await query.edit_message_text(
            text=f"{query.message.caption or query.message.text}\n\n❌ <b>Error al enviar al webhook.</b>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True # Match original setting
        )


# --- Test Command Handler ---
async def test_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /test command to send a dummy article for approval."""
    user = update.effective_user
    chat = update.effective_chat
    bot = context.bot

    logger.info(f"Comando /test recibido de {user.username or user.first_name} en chat {chat.id}")

    # Crear datos dummy
    timestamp = int(time.time())
    dummy_title = f"[TEST] Noticia de Prueba - {timestamp}"
    dummy_url = f"urn:script:test:{timestamp}" # URL única para la prueba
    dummy_summary = f"Este es un resumen de prueba generado por el comando /test a las {time.strftime('%Y-%m-%d %H:%M:%S')}."
    dummy_placeholder_image_url = "https://via.placeholder.com/1024x1024.png?text=Test+Image" # Placeholder image
    dummy_source_feed = "Comando /test"

    logger.info(f"Generando artículo de prueba: '{dummy_title}'")

    # Intentar generar y subir una imagen real para la prueba
    generated_imgbb_url = None
    if IMAGE_GENERATION_ENABLED:
        logger.info("Intentando generar y subir imagen de prueba...")
        test_prompt = f"A simple, visually appealing graphic representing a 'Test News Article' generated at {timestamp}. Include a checkmark or a gear icon."
        # Call the function which now uploads to ImgBB
        generated_imgbb_url = await generate_and_upload_image_via_script_async(test_prompt)
        if generated_imgbb_url:
            logger.info(f"Imagen de prueba generada y subida a ImgBB exitosamente: {generated_imgbb_url}")
        else:
            logger.warning("Fallo al generar o subir la imagen de prueba a ImgBB, se usará placeholder.")

    # Use ImgBB URL or placeholder
    final_image_url_for_test = generated_imgbb_url or dummy_placeholder_image_url

    # Añadir a la base de datos para obtener un ID, usando la URL final
    article_id = await add_article_async(
        dummy_url,
        dummy_title,
        dummy_summary,
        final_image_url_for_test, # Saves ImgBB URL or placeholder
        dummy_source_feed
    )

    if not article_id:
        logger.error("No se pudo añadir el artículo de prueba a la base de datos.")
        await update.message.reply_text("❌ Error: No se pudo guardar el artículo de prueba en la base de datos.")
        # No temporary file to clean up here
        return

    logger.info(f"Artículo de prueba añadido a BD con ID: {article_id}. Enviando a Telegram para aprobación...")

    # Enviar al chat de Telegram configurado para aprobación, usando la URL final
    success = await send_to_telegram_chat(
        bot=bot,
        chat_id=TELEGRAM_CHAT_ID,
        article_id=article_id,
        title=dummy_title,
        url=dummy_url,
        summary=dummy_summary,
        image_url=final_image_url_for_test # Pasa la URL de ImgBB o la URL placeholder
    )

    if success:
        logger.info(f"Artículo de prueba (ID: {article_id}) enviado correctamente a {TELEGRAM_CHAT_ID}.")
        # Informar al usuario que lo ejecutó (en el chat original)
        await update.message.reply_text(f"✅ ¡Artículo de prueba enviado al chat configurado ({TELEGRAM_CHAT_ID}) para aprobación!")
    else:
        logger.error(f"Fallo al enviar el artículo de prueba (ID: {article_id}) a Telegram ({TELEGRAM_CHAT_ID}).")
        await update.message.reply_text(f"❌ Error: No se pudo enviar el artículo de prueba al chat configurado ({TELEGRAM_CHAT_ID}). Revisa los logs.")
        # Note: send_to_telegram_chat should handle cleanup of temp image if it failed sending it


# --- Main Job Function (Async) ---
async def check_news_cycle(context: ContextTypes.DEFAULT_TYPE):
    """The main async job function to check feeds and send to Telegram."""
    logger.info("Starting news check cycle...")
    # Fetch recent articles *within* the cycle if deduplication is needed per-cycle
    # Otherwise, it's mainly for the AI check
    recent_db_articles = await get_recent_articles_async()
    processed_articles_in_cycle = 0
    bot = context.bot # Get bot instance from context

    for feed_url in NEWS_URLS:
        logger.info(f"Checking feed: {feed_url}")
        processed_urls_this_feed = set() # Track URLs processed in *this specific feed check*

        try:
            # Use a User-Agent
            headers = {'User-Agent': f'AINewsRecolektor/1.0 ({WEBHOOK_URL})'}
            feed_data = await asyncio.to_thread(
                feedparser.parse, feed_url,
                request_headers=headers,
                etag=None, # Consider adding etag/modified handling if needed
                agent=headers['User-Agent'] # feedparser uses 'agent'
            )
            # Reset default socket timeout just in case feedparser changed it
            # socket.setdefaulttimeout(None) # Probably not needed with asyncio.to_thread

            if feed_data.bozo:
                 logger.warning(f"Feed potentially malformed: {feed_url}. Reason: {feed_data.bozo_exception}")
            if not feed_data.entries:
                 logger.debug(f"No entries found in feed: {feed_url}")
                 continue

            logger.debug(f"Found {len(feed_data.entries)} entries in feed: {feed_url}")

            for entry in feed_data.entries:
                article_url = getattr(entry, 'link', None)
                article_title = getattr(entry, 'title', None)
                if not article_url or not article_title: continue

                article_url = urljoin(feed_url, article_url)
                if article_url in processed_urls_this_feed: continue # Avoid processing same URL multiple times in one fetch
                processed_urls_this_feed.add(article_url)

                if await article_exists_async(article_url):
                    # logger.debug(f"Article already in DB: {article_url}") # Can be very verbose
                    continue

                logger.info(f"Found potential new article: '{article_title}' ({article_url})")

                summary = getattr(entry, 'summary', getattr(entry, 'description', None))
                original_image_url = None
                # Extract image URL from feed entry more robustly
                if 'media_content' in entry and entry.media_content:
                     img_urls = [item.get('url') for item in entry.media_content if item.get('medium') == 'image' and item.get('url')]
                     if img_urls: original_image_url = img_urls[0]
                if not original_image_url and 'links' in entry:
                    for link in entry.links:
                         if link.get('rel') == 'enclosure' and link.get('type','').startswith('image/'):
                            original_image_url = link.get('href'); break
                # Fallback: Check for images in summary HTML (less reliable)
                if not original_image_url and summary:
                    try:
                        img_soup = BeautifulSoup(summary, 'html.parser')
                        img_tag = img_soup.find('img')
                        if img_tag and img_tag.get('src'):
                            original_image_url = urljoin(article_url, img_tag['src'])
                            logger.debug(f"Extracted image from summary/description: {original_image_url}")
                    except Exception: pass # Ignore parsing errors

                cleaned_summary = summary
                if cleaned_summary: # Clean summary *after* trying to extract image from it
                    try:
                        soup = BeautifulSoup(cleaned_summary, 'html.parser')
                        # Remove potentially problematic tags but keep basic formatting if desired
                        for tag in soup(["script", "style", "iframe"]):
                            tag.decompose()
                        # Get text, preserving line breaks from <p>, <br> might be useful
                        cleaned_summary = soup.get_text(separator='\n', strip=True)
                        # Unescape HTML entities last
                        cleaned_summary = html.unescape(cleaned_summary)
                        logger.debug(f"Cleaned summary for: {article_url}")
                    except Exception as e:
                        logger.warning(f"Could not clean summary for {article_url}: {e}")
                        cleaned_summary = html.unescape(summary) # Basic unescape as fallback


                is_ai_duplicate = await is_duplicate_async(article_title, cleaned_summary, recent_db_articles)
                if is_ai_duplicate:
                     logger.info(f"[AI DEDUPE] Article determined as duplicate, skipping: {article_title}")
                     continue

                # Generate and upload image using the script if needed
                generated_imgbb_url = None
                if not original_image_url and IMAGE_GENERATION_ENABLED: # Only generate if no original image
                     image_prompt = ( # Construct the prompt here
                         f"Generate an engaging Instagram-style thumbnail representing the following news article. "
                         f"Focus on the core subject visually. Avoid text, or use at most 1-2 relevant words clearly. "
                         f"News Title: {article_title}. Summary: {(cleaned_summary or '')[:500]}..."
                     )
                     generated_imgbb_url = await generate_and_upload_image_via_script_async(image_prompt)

                # Determine final image URL for DB and Telegram
                # Priority: Generated ImgBB URL > Original feed URL
                final_image_url = generated_imgbb_url or original_image_url

                # Add article to DB *first* to get an ID
                # The 'image_url' column will store the final URL (ImgBB or original)
                article_id = await add_article_async(article_url, article_title, cleaned_summary, final_image_url, feed_url)

                if not article_id:
                     logger.warning(f"Failed to add article to DB for: {article_url}. Skipping Telegram send.")
                     # No temporary file to clean up
                     continue

                # Send to Telegram using the final URL
                success = await send_to_telegram_chat(
                    bot=bot,
                    chat_id=TELEGRAM_CHAT_ID,
                    article_id=article_id,
                    title=article_title,
                    url=article_url,
                    summary=cleaned_summary,
                    image_url=final_image_url # Pass the final URL
                )

                if success:
                    processed_articles_in_cycle += 1
                    # Update recent articles list for *next* AI check if needed
                    recent_db_articles.insert(0, {'title': article_title, 'url': article_url}) # Keep this for dedupe context
                    if len(recent_db_articles) > MAX_RECENT_ARTICLES_FOR_CHECK: recent_db_articles.pop()
                    logger.info(f"Article ID {article_id} sent to Telegram for approval.")
                else:
                     logger.error(f"Failed to send article ID {article_id} to Telegram.")
                     # Note: send_to_telegram_chat might have already cleaned up the image on failure to send local file

                await asyncio.sleep(1.5) # Be nice

            logger.info(f"Finished checking {feed_url}. Found {len(processed_urls_this_feed)} potential articles. Sent {processed_articles_in_cycle} new unique articles to Telegram in this cycle so far.")

        except socket.timeout:
            logger.error(f"Timeout error fetching feed: {feed_url}")
        except Exception as e:
            logger.error(f"Failed to process feed URL {feed_url}: {e}", exc_info=True)

        await asyncio.sleep(2) # Wait between feeds

    logger.info(f"News check cycle finished. Sent {processed_articles_in_cycle} new articles to Telegram in this cycle.")


# --- Post-Initialization Function for Bot ---
async def post_init(application: Application):
    """Runs after bot initialization and schedules the job."""
    # Removed the startup test message call
    # await send_test_telegram_message(application.bot)
    logger.info("Bot inicializado. Programando ciclo de revisión de noticias...")

    # Schedule the news check job
    job_queue = application.job_queue
    job_queue.run_repeating(
        check_news_cycle,
        interval=CHECK_INTERVAL_SECONDS,
        first=10, # Run first check 10 seconds after startup
        name="check_news_cycle_job"
    )
    logger.info(f"Scheduled news check job to run every {CHECK_INTERVAL_SECONDS} seconds.")
    logger.info("Bot listo y JobQueue iniciado.")


# --- Main Execution ---
if __name__ == "__main__":
    logger.info("Inicializando script...")
    init_db()
    logger.info("Ejecutando poblado inicial de la base de datos...")
    populate_initial_db() # Run initial population synchronously before starting bot

    logger.info("Configurando y iniciando el bot de Telegram...")

    # Build the application and set timeouts here
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        # Set timeouts for the HTTP client used by the bot
        .connect_timeout(TELEGRAM_TIMEOUT)
        .read_timeout(TELEGRAM_TIMEOUT)
        .write_timeout(TELEGRAM_TIMEOUT)
        .post_init(post_init) # Function to run after init
        .concurrent_updates(True) # Handle multiple updates concurrently
        .build()
    )

    # --- Add Handlers ---
    # Handler for button presses
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    # Handler for the /test command
    application.add_handler(CommandHandler("test", test_command_handler))

    logger.info("Iniciando polling del bot...")
    # Run the bot until the user presses Ctrl-C
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.critical(f"Error crítico en el polling del bot: {e}", exc_info=True)
    finally:
        logger.info("Deteniendo el bot.")
        # asyncio tasks should be cancelled automatically by run_polling on exit

    logger.info("Script terminado.")
