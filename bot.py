#!/usr/bin/env python3
"""Telegram-бот: ссылка на YouTube -> аудио (m4a, без перекодирования).

Зависимости:  pip install -U aiogram "yt-dlp[default]"   + ffmpeg (с ffprobe) и deno в PATH
Переменные:   BOT_TOKEN, OWNER_ID, опционально COOKIES_FILE (cookies.txt для YouTube)
"""
import asyncio
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.types import FSInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
COOKIES_FILE = os.environ.get("COOKIES_FILE")

MAX_BYTES = 49 * 1024 * 1024   # лимит облачного Bot API - 50 МБ
PART_BYTES = 45 * 1024 * 1024  # целевой размер части при нарезке
YT_RE = re.compile(r"https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/\S+")

dp = Dispatcher()
dp.message.filter(F.from_user.id == OWNER_ID)  # чужие сообщения игнорируются


def download(url: str, tmp: Path) -> tuple[dict, Path]:
    """Качает готовую аудиодорожку. Блокирующая, вызывать в потоке."""
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "paths": {"home": str(tmp)},
        "outtmpl": "%(id)s.%(ext)s",
        "noplaylist": True,
        "playlist_items": "1",  # если пришла ссылка на плейлист - только первое видео
        "match_filter": yt_dlp.utils.match_filter_func("!is_live"),
        "quiet": True,
        "no_warnings": True,
        "color": "never",
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("entries"):
        info = info["entries"][0]
    files = info.get("requested_downloads")
    if not files:
        raise RuntimeError("нечего скачивать (идёт стрим или видео недоступно)")
    return info, Path(files[0]["filepath"])


def split(src: Path, duration: float) -> list[tuple[Path, int]]:
    """Возвращает [(файл, длительность)]. Большой файл режет ffmpeg'ом без перекодирования."""
    size = src.stat().st_size
    if size <= MAX_BYTES:
        return [(src, int(duration))]
    # точная длительность самого файла (в метаданных YouTube она округлена)
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=True,
    ).stdout)
    n = math.ceil(size / PART_BYTES)
    seg = math.ceil(duration / n) + 1  # +1 с, иначе ffmpeg выдаёт лишний хвост в долю секунды
    out = src.parent / "parts"
    out.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-map", "0:a", "-c", "copy",
         "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
         str(out / f"%02d{src.suffix}")],
        check=True,
    )
    parts = sorted(out.iterdir())
    return [(p, max(1, round(min(seg, duration - seg * i)))) for i, p in enumerate(parts)]


@dp.message(F.text)
async def on_message(message: Message) -> None:
    m = YT_RE.search(message.text)
    if not m:
        await message.answer("Пришли ссылку на YouTube.")
        return
    bot, chat = message.bot, message.chat.id
    try:
        async with ChatActionSender.upload_voice(bot=bot, chat_id=chat):
            with tempfile.TemporaryDirectory() as tmp:
                info, src = await asyncio.to_thread(download, m.group(0), Path(tmp))
                parts = await asyncio.to_thread(split, src, info.get("duration") or 0)
                title = info.get("title") or src.stem
                for i, (path, dur) in enumerate(parts, 1):
                    name = title if len(parts) == 1 else f"{title} ({i}/{len(parts)})"
                    file = FSInputFile(path, filename=re.sub(r'[\\/:*?"<>|]', "_", name) + path.suffix)
                    # таймаут задаётся только через bot.send_*: дефолтных 60 с на 45 МБ может не хватить
                    if path.suffix in (".m4a", ".mp3"):
                        await bot.send_audio(
                            chat, file, title=name, performer=info.get("uploader"),
                            duration=dur, request_timeout=600,
                        )
                    else:  # m4a у видео не оказалось - отдаём как файл
                        await bot.send_document(chat, file, request_timeout=600)
    except Exception as e:
        await message.answer(f"Не получилось: {e}"[:4000])


async def main() -> None:
    await dp.start_polling(Bot(BOT_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
