import html
import json
import logging
import os
import traceback
from io import StringIO
from tempfile import TemporaryFile
from typing import Optional
from urllib.parse import urlsplit

import requests

try:
    import re2 as re
except ImportError:
    import re

import telegram.error
from telegram import Update, InputMediaDocument, InputMediaAnimation, constants, Bot, BotCommand, BotCommandScopeChat
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, PicklePersistence

BOT_TOKEN = Bot(token=os.environ.get('token'))
DEVELOPER_ID = 366858436
IS_BOT_PRIVATE = False

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class APIException(Exception):
    pass

def extract_tweet_ids(update: Update) -> Optional[list[str]]:
    """Extract tweet IDs from message."""
    text = update.effective_message.text

    # For t.co links
    unshortened_links = ''
    for link in re.findall(r"t\.co\/[a-zA-Z0-9]+", text):
        try:
            unshortened_link = requests.get('https://' + link).url
            unshortened_links += '\n' + unshortened_link
            log_handling(update, 'info', f'Unshortened t.co link [https://{link} -> {unshortened_link}]')
        except:
            log_handling(update, 'info', f'Could not unshorten link [https://{link}]')

    # Parse IDs from received text
    tweet_ids = re.findall(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", text + unshortened_links)
    tweet_ids = list(dict.fromkeys(tweet_ids))
    return tweet_ids or None

def scrape_media(tweet_id: int) -> list[dict]:
    r = requests.get(f'https://api.vxtwitter.com/Twitter/status/{tweet_id}')
    r.raise_for_status()
    try:
        return r.json()['media_extended']
    except requests.exceptions.JSONDecodeError: # the api likely returned an HTML page, try looking for an error message
        # <meta content="{message}" property="og:description" />
        if match := re.search(r'<meta content="(.*?)" property="og:description" />', r.text):
            raise APIException(f'API returned error: {html.unescape(match.group(1))}')
        raise

def reply_media(update: Update, context: CallbackContext, tweet_media: list) -> bool:
    """Reply to message with supported media."""
    photos = [media for media in tweet_media if media["type"] == "image"]
    gifs = [media for media in tweet_media if media["type"] == "gif"]
    videos = [media for media in tweet_media if media["type"] == "video"]
    if photos:
        reply_photos(update, context, photos)
    if gifs:
        reply_gifs(update, context, gifs)
    elif videos:
        reply_videos(update, context, videos)
    return bool(photos or gifs or videos)

def reply_photos(update: Update, context: CallbackContext, twitter_photos: list[dict]) -> None:
    """Reply with photo group."""
    photo_group = []
    for photo in twitter_photos:
        photo_url = photo['url']
        log_handling(update, 'info', f'Photo[{len(photo_group)}] url: {photo_url}')
        parsed_url = urlsplit(photo_url)

        # Try changing requested quality to 'orig'
        try:
            new_url = parsed_url._replace(query='format=jpg&name=orig').geturl()
            log_handling(update, 'info', 'New photo url: ' + new_url)
            requests.head(new_url).raise_for_status()
            photo_group.append(InputMediaDocument(media=new_url))
        except requests.HTTPError:
            log_handling(update, 'info', 'orig quality not available, using original url')
            photo_group.append(InputMediaDocument(media=photo_url))
    update.effective_message.reply_media_group(photo_group, quote=True)
    log_handling(update, 'info', f'Sent photo group (len {len(photo_group)})')
    context.bot_data['stats']['media_downloaded'] += len(photo_group)

def reply_gifs(update: Update, context: CallbackContext, twitter_gifs: list[dict]):
    """Reply with GIF animations."""
    for gif in twitter_gifs:
        gif_url = gif['url']
        log_handling(update, 'info', f'Gif url: {gif_url}')
        update.effective_message.reply_animation(animation=gif_url, quote=True)
        log_handling(update, 'info', 'Sent gif')
        context.bot_data['stats']['media_downloaded'] += 1

def reply_videos(update: Update, context: CallbackContext, twitter_videos: list[dict]):
    """Reply with videos."""
    for video in twitter_videos:
        video_url = video['url']
        try:
            request = requests.get(video_url, stream=True)
            request.raise_for_status()
            if (video_size := int(request.headers['Content-Length'])) <= constants.MAX_FILESIZE_DOWNLOAD:
                # Try sending by url
                update.effective_message.reply_video(video=video_url, quote=True)
                log_handling(update, 'info', 'Sent video (download)')
            elif video_size <= constants.MAX_FILESIZE_UPLOAD:
                log_handling(update, 'info', f'Video size ({video_size}) is bigger than '
                                            f'MAX_FILESIZE_UPLOAD, using upload method')
                message = update.effective_message.reply_text(
                    'Video is too large for direct download\nUsing upload method '
                    '(this might take a bit longer)',
                    quote=True)
                with TemporaryFile() as tf:
                    log_handling(update, 'info', f'Downloading video (Content-length: '
                                                f'{request.headers["Content-length"]})')
                    for chunk in request.iter_content(chunk_size=128):
                        tf.write(chunk)
                    log_handling(update, 'info', 'Video downloaded, uploading to Telegram')
                    tf.seek(0)
                    update.effective_message.reply_video(video=tf, quote=True, supports_streaming=True)
                    log_handling(update, 'info', 'Sent video (upload)')
                message.delete()
            else:
                log_handling(update, 'info', 'Video is too large, sending direct link')
                update.effective_message.reply_text(f'Video is too large for Telegram upload. Direct video link:\n'
                                        f'{video_url}', quote=True)
        except (requests.HTTPError, KeyError, telegram.error.BadRequest, requests.exceptions.ConnectionError) as exc:
            log_handling(update, 'info', f'{exc.__class__.__qualname__}: {exc}')
            log_handling(update, 'info', 'Error occurred when trying to send video, sending direct link')
            update.effective_message.reply_text(f'Error occurred when trying to send video. Direct link:\n'
                                    f'{video_url}', quote=True)
        context.bot_data['stats']['media_downloaded'] += 1

# TODO: use LoggerAdapter instead
def log_handling(update: Update, level: str, message: str) -> None:
    """Log message with chat_id and message_id."""
    _level = getattr(logging, level.upper())
    logger.log(_level, f'[{update.effective_chat.id}:{update.effective_message.message_id}] {message}')

def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""

    if isinstance(context.error, telegram.error.Unauthorized):
        return

    if isinstance(context.error, telegram.error.Conflict):
        logger.error("Telegram requests conflict")
        return

    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    
    # if there is no update, don't send an error report (probably a network error, happens sometimes)
    if update is None:
        return

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'#error_report\n'
        f'An exception was raised in runtime\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # send the message
    try:
        context.bot.send_message(chat_id=DEVELOPER_ID, text=message, parse_mode=telegram.ParseMode.HTML)
    except telegram.error.BadRequest as excp:
        if 'Entity too large' in str(excp):
            with StringIO() as output:
                output.write(message)
                output.seek(0)
                context.bot.send_document(chat_id=DEVELOPER_ID, document=output, filename='error.html')
        else:
            raise

def main() -> None:
    # Load data persistence
    persistence = PicklePersistence('bot_data')

    # Create the Updater and pass it your bot's token.
    updater = Updater(BOT_TOKEN, persistence=persistence)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # on different commands - answer in Telegram
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("stats", stats_command))

    # on non command text messages
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, process_tweet, run_async=True))

    # log all errors
    dispatcher.add_error_handler(error_handler)

    # Start the Bot
    updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT, SIGTERM or SIGABRT
    updater.idle()

def start_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    update.message.reply_text('Hi!')

def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    update.message.reply_text('Help!')

def stats_command(update: Update, context: CallbackContext) -> None:
    """Send a message with the bot's statistics."""
    update.message.reply_text(f"Media downloaded: {context.bot_data.get('stats', {}).get('media_downloaded', 0)}")

def process_tweet(update: Update, context: CallbackContext) -> None:
    """Process tweets from the received message."""
    tweet_ids = extract_tweet_ids(update)
    if tweet_ids is None:
        update.message.reply_text('No valid tweet IDs found.')
        return

    for tweet_id in tweet_ids:
        try:
            tweet_media = scrape_media(tweet_id)
            if not reply_media(update, context, tweet_media):
                update.message.reply_text('No supported media found.')
        except requests.HTTPError as exc:
            update.message.reply_text(f'HTTP Error: {exc}')
        except APIException as exc:
            update.message.reply_text(f'API Exception: {exc}')
        except Exception as exc:
            update.message.reply_text(f'An unexpected error occurred: {exc}')
            log_handling(update, 'error', f'Exception: {exc}')

if __name__ == '__main__':
    main()
