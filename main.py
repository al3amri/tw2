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
from flask import Flask
from keep_alive import keep_alive
keep_alive()

try:
    import re2 as re
except ImportError:
    import re

import telegram.error
from telegram import Update, InputMediaDocument, InputMediaAnimation, constants, Bot, BotCommand, BotCommandScopeChat
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, PicklePersistence

app = Flask(__name__)  # Flask app for health check

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
    tb_string = "".join(tb_list)

    # Build the message with some markup, to give it a little polish.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    # Finally, send the message
    try:
        context.bot.send_message(chat_id=DEVELOPER_ID, text=message, parse_mode=telegram.ParseMode.HTML)
    except telegram.error.TelegramError as ex:
        logger.error(f'Error while sending error message: {ex}')
        return

def stats_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the /stats command is issued."""
    stats = context.bot_data['stats']
    update.message.reply_text(
        f'Files downloaded: {stats["media_downloaded"]}\n'
        f'Errors: {stats["errors"]}'
    )

def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the /help command is issued."""
    update.message.reply_text('Send me tweet links and I will reply with any media I can find')

def command_unknown(update: Update, context: CallbackContext) -> None:
    """Send a message when the user sends an unknown command."""
    update.message.reply_text('Sorry, I didn\'t understand that command.')

def main() -> None:
    """Start the bot."""
    persistence = PicklePersistence('bot_data', single_file=False)

    # Create the Updater and pass it your bot's token.
    updater = Updater(BOT_TOKEN.token, persistence=persistence, use_context=True)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # Register /stats command
    dispatcher.add_handler(CommandHandler("stats", stats_command))

    # on noncommand i.e message - send tweet link message on Telegram
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, tweet_handler))
    
    # Register an unknown command handler
    dispatcher.add_handler(MessageHandler(Filters.command, command_unknown))

    # log all errors
    dispatcher.add_error_handler(error_handler)

    updater.job_queue.run_repeating(save_stats, interval=constants.DAY)

    # Start the Bot
    updater.start_polling()
    updater.idle()

@app.route('/health')
def health():
    return 'Bot is running'

if __name__ == '__main__':
    main()
    app.run(host='0.0.0.0', port=8080)
