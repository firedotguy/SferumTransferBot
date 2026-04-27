from os import name as os_name, getenv
from asyncio import run, wait, create_task, FIRST_COMPLETED, Event, get_running_loop, sleep
from logging import getLogger
from signal import SIGINT, SIGTERM
from time import time as time_since_epoch
from io import BytesIO
from html import escape as he
from typing import TypeVar, Callable, Any
from types import CoroutineType

from dotenv import load_dotenv

from aiohttp.client_exceptions import ClientConnectorError
from aiohttp import ClientSession
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InputMediaPhoto, MediaUnion, InputMediaVideo, ReactionTypeEmoji

from pymax import SocketMaxClient, MaxClient, Message
from pymax.types import PhotoAttach, VideoAttach, FileAttach, StickerAttach
from pymax.filters import Filters
from pymax.files import Photo

from gzip import compress

import data_handler
from logger import setup_logger

# --- Initial Setup ---
setup_logger()
l = getLogger(__name__)
load_dotenv()


REQUESTS_TIMEOUT = 15 # таймаут запросов

# --- Environment Variables ---
try:
    USE_SOCKET_CLIENT = eval(getenv('USE_SOCKET_CLIENT', 'False').title())
    MAX_PHONE = getenv('VK_PHONE')
    MAX_CHAT_ID = int(getenv('VK_CHAT_ID', 0))
    MAX_TOKEN = getenv('VK_COOKIE')
    TG_CHAT_ID = int(getenv('TG_CHAT_ID', 0))
    TG_TOKEN = getenv('TG_TOKEN')
    ADMIN_USER_ID = int(getenv('ADMIN_USER_ID', 0))
    if not all([MAX_CHAT_ID, TG_CHAT_ID, TG_TOKEN, MAX_TOKEN, MAX_PHONE]):
        raise ValueError("One or more environment variables are not set.")

    assert TG_TOKEN
    assert MAX_PHONE
except (ValueError, TypeError) as e:
    l.critical(f"FATAL: Configuration error - {e}. Please check your .env file.")
    quit(1)

msgs_map = data_handler.load('msgs') or {}
last_sender_id = None
sent_by_bot: set[int] = set() # id сообщений отправленных через /send

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

# Reconnect=True effectively replaces the "Watchdog" thread
if USE_SOCKET_CLIENT:
    client = SocketMaxClient(MAX_PHONE, token=MAX_TOKEN, work_dir="data/cache", reconnect=True)
else:
    client = MaxClient(MAX_PHONE, token=MAX_TOKEN, work_dir="data/cache", reconnect=True)

T = TypeVar('T')

async def tg_retry[T](func: Callable[..., CoroutineType[Any, Any, T]] | Callable[..., T], *args, retries: int = 10, **kwargs) -> T:
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs) # pyright: ignore[reportGeneralTypeIssues]
        except (TelegramNetworkError, ClientConnectorError) as e:
            if attempt == retries - 1:
                raise e
            l.warning(f"request failed attempt={attempt + 1}: {e}")
            await sleep(3)
    raise


async def download_content(url: str) -> BytesIO:
    """Download content from URL into memory."""
    async with ClientSession() as session:
        async with session.get(url, timeout=REQUESTS_TIMEOUT) as response: # pyright: ignore[reportArgumentType]
            response.raise_for_status()
            content = await response.read()
            file_bytes = BytesIO(content)
            # Attempt to set a name, though Telegram often overrides logic based on method
            file_bytes.name = response.headers.get("X-File-Name", "file")
            return file_bytes

async def get_sender_name(id: int | None) -> str:
    """Fetch user name via PyMax."""
    if id is None:
        return 'неизвестный'
    user = await client.get_user(user_id=id)
    if user and user.names:
        return he(user.names[0].name or '')
    return str(id)



async def process_max_message(message: Message) -> None:
    if message.id in sent_by_bot:
        return

    text = f'<b>{await get_sender_name(message.sender)} отправил(-a):</b>\n{he(message.text or '')}'

    reply = None
    if message.link and message.link.type == 'REPLY':
        reply = msgs_map.get(str(message.link.message.id))
        if reply is None:
            text = '<i><- Ответ на неизвестное сообщение</i>\n' + text

    if message.link and message.link.type == 'FORWARD':
        text = f'<i>-> Переслано от {await get_sender_name(message.link.message.sender)}</i>\n' + text + message.link.message.text

    photos: list[BufferedInputFile] = []
    video: BufferedInputFile | None = None
    file: BufferedInputFile | None = None
    sticker: BufferedInputFile | None = None
    for attach in message.attaches or []:
        if isinstance(attach, PhotoAttach):
            photos.append(BufferedInputFile((await download_content(attach.base_url)).getvalue(), 'photo.jpg'))
        elif isinstance(attach, VideoAttach):
            video_data = await client.get_video_by_id(MAX_CHAT_ID, message.id, attach.video_id)
            if video_data:
                video = BufferedInputFile((await download_content(video_data.url)).getvalue(), 'video.mp4')
        elif isinstance(attach, FileAttach):
            file_data = await client.get_file_by_id(MAX_CHAT_ID, message.id, attach.file_id)
            if file_data:
                file = BufferedInputFile((await download_content(file_data.url)).getvalue(), attach.name)
        elif isinstance(attach, StickerAttach):
            # photos.append(BufferedInputFile((await download_content(attach.url)).getvalue(), 'sticker.png'))
            if attach.lottie_url:
                sticker = BufferedInputFile(compress((await download_content(attach.lottie_url)).getvalue()), 'sticker.tgs')
            else:
                photos.append(BufferedInputFile((await download_content(attach.url)).getvalue(), 'sticker.png'))
            text += '<i>Стикер</i>'
        else:
            text += '<i>Неизвестное вложение</i>'

    if len(photos) == 1:
        tg_message = await bot.send_photo(TG_CHAT_ID, photos[0], caption=text, reply_to_message_id=reply)
    elif photos:
        media: list[MediaUnion] = [InputMediaPhoto(media=photo) for photo in photos]
        media[0].caption = text
        if video:
            media.append(InputMediaVideo(media=video))
        tg_message = (await tg_retry(bot.send_media_group, TG_CHAT_ID, media, reply_to_message_id=reply))[0]
    elif video:
        tg_message = await tg_retry(bot.send_video, TG_CHAT_ID, video, caption=text, reply_to_message_id=reply)
    elif file:
        tg_message = await tg_retry(bot.send_document, TG_CHAT_ID, file, caption=text, reply_to_message_id=reply)
    elif sticker:
        await bot.send_message(TG_CHAT_ID, text)
        tg_message = await tg_retry(bot.send_sticker, TG_CHAT_ID, sticker, reply_to_message_id=reply)
    else:
        tg_message = await tg_retry(bot.send_message, TG_CHAT_ID, text, reply_to_message_id=reply)

    l.info('receive message text=%s photos=%s video=%s file=%s', text.replace('\n', '  '), len(photos), bool(video), bool(file))
    msgs_map[str(message.id)] = tg_message.message_id
    data_handler.save('msgs', msgs_map)
    await client.read_message(message.id, MAX_CHAT_ID)


@client.on_message(Filters.chat(MAX_CHAT_ID))
async def max_message_handler(message: Message):
    # PyMax entry point
    await process_max_message(message)

@client.on_start
async def on_start():
    chat = await client.get_chat(MAX_CHAT_ID)
    if chat.new_messages > 0:
        messages = await client.fetch_history(chat.id, int(time_since_epoch() * 1000), 0, chat.new_messages) or []
        l.info(f"fetched {len(messages)} messages")
        for message in messages:
            if str(message.id) in msgs_map:
                l.info(f"skip already forwarded message {message.id}")
                continue
            message.chat_id = chat.id
            await process_max_message(message)

# --- Logic: Telegram -> Max ---


@dp.message(Command("send"))
async def send_handler(message: types.Message):
    """Handles /send command."""
    assert message.from_user
    try:
        if ADMIN_USER_ID and message.from_user.id != ADMIN_USER_ID:
            await tg_retry(message.reply, 'Отправка сообщений доступна только администратору')
            return

        # Check empty message
        text = (message.text or '').replace("/send", "", 1).strip()

        # Get id of replied message in MAX
        reply = None
        if message.reply_to_message:
            reply = next((max_id for max_id, tg_id in msgs_map.items() if tg_id == message.reply_to_message.message_id), None)

        photo = None
        if message.photo:
            photo_data = await bot.download(message.photo[-1])
            if photo_data:
                photo = Photo(photo_data.read(), path='photo.jpg')

        sent_msg = await client.send_message(text, MAX_CHAT_ID, attachment=photo, reply_to=reply)

        # Map message
        if sent_msg and sent_msg.id:
            msgs_map[str(sent_msg.id)] = message.message_id
            sent_by_bot.add(sent_msg.id)
            await tg_retry(message.react, [ReactionTypeEmoji(emoji='👍')])

    except Exception as e:
        l.error(f"Error in send_handler: {e}", exc_info=True)
        await tg_retry(message.reply, 'Произошла ошибка при отправке.')

@dp.message()
async def reply_hansler(message: types.Message):
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        await send_handler(message)

async def main():
    stop_event = Event()
    loop = get_running_loop()
    if os_name != 'nt':
        for sig in (SIGINT, SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

    async def tg_polling_with_retry():
        while not stop_event.is_set():
            await tg_retry(dp.start_polling, bot)

    tg_task = create_task(tg_polling_with_retry(), name='tg')
    max_task = create_task(client.start(), name='max')
    stop_task = create_task(stop_event.wait(), name='stop')
    l.info('start bot')

    done, _ = await wait(
        [tg_task, max_task, stop_task],
        return_when=FIRST_COMPLETED
    )
    for task in done:
        exc = task.exception() if not task.cancelled() else None
        l.error(f"{task.get_name()} finished exc={exc!r}")

    tg_task.cancel()
    max_task.cancel()

    await client.close()
    await bot.session.close()

try:
    run(main())
except (KeyboardInterrupt, SystemExit):
    l.info("stop bot")
