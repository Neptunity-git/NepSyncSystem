"""人のアイコンを、bot 専用の絵文字（アプリの絵文字）にして、文の中に小さく出す（ADR 0004 判断8）。

- 絵文字の名前は nep_<人の ID>_<a か b>（サーバーごとにアイコンが違うことがあるため）
- アイコンが変わったら作り直す。Neplector から外れた人と、サーバーにいなくなった人の分は消す
- 長いあいだ表示されていない分も消す（既定 90日。要るときにまた作る）
- 手元に残すのは、絵文字の ID と、アイコンの URL のハッシュだけ
- 失敗しても名前だけで出せばよいので、例外は外に出さない
- 作る・消すは discord.py の Client（create_application_emoji など）"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse
from typing import Any, Awaitable, Callable

import discord

from .neplector import NeplectorError, Transient

MAX_BYTES = 256 * 1024        # Discord の決まり
SIZE = 128                    # Discord の資料：絵文字の画像は 128x128
PREFIX = "nep_"
FAILURES = (discord.HTTPException, NeplectorError, Transient)


def sized_url(url: str) -> str:
    """CDN のアイコンを、128 の PNG で取る形にする（動くアイコンも止まった絵にする）。"""
    parts = urllib.parse.urlsplit(url)
    path = parts.path
    for suffix in (".gif", ".webp", ".jpg", ".jpeg"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] + ".png"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, f"size={SIZE}", ""))


class AvatarEmojis:
    def __init__(self, client: Any, store: Any, guild_tags: dict[str, str],
                 download: Callable[[str], Awaitable[bytes]], log: Callable[[str], None] = print, *,
                 keep_days: int = 90, clock: Callable[[], float] = time.time) -> None:
        self.client, self.store = client, store
        self.guild_tags, self.download, self.log = guild_tags, download, log
        self.keep_days, self.clock = keep_days, clock
        self._lock = asyncio.Lock()   # コマンドと同期の本体が同時に作り直さないように

    async def emoji_for(self, guild_id: str, user_id: str, avatar_url: str | None) -> str:
        """文に入れる形（<:名前:ID>）。作れないときは空の文字列。"""
        if not avatar_url or guild_id not in self.guild_tags or not user_id.isdigit():
            return ""
        async with self._lock:
            return await self._emoji_for(guild_id, user_id, avatar_url)

    async def _emoji_for(self, guild_id: str, user_id: str, avatar_url: str) -> str:
        digest = hashlib.sha256(avatar_url.encode()).hexdigest()[:16]
        saved = self.store.avatar_emoji(guild_id, user_id)
        if saved and saved["digest"] == digest:
            self.store.touch_avatar_emoji(guild_id, user_id, self.clock())
            return f"<:{saved['name']}:{saved['emoji_id']}>"
        name = f"{PREFIX}{user_id}_{self.guild_tags[guild_id]}"
        try:
            if saved:
                await self._delete(saved["emoji_id"])
                self.store.drop_avatar_emoji(guild_id, user_id)
            data = await self.download(sized_url(avatar_url))
            if len(data) > MAX_BYTES:
                return ""
            try:
                made = await self.client.create_application_emoji(name=name, image=data)
            except discord.HTTPException as error:
                if error.status != 400:
                    raise
                # 控えを失ったなどで、同じ名前が残っている。消して作り直す（名前は1つしか持てない）
                for item in await self.client.fetch_application_emojis():
                    if item.name == name:
                        await item.delete()
                made = await self.client.create_application_emoji(name=name, image=data)
        except FAILURES as error:
            self.log(f"アイコンの絵文字を作れませんでした: {type(error).__name__}")
            return ""
        self.store.save_avatar_emoji(guild_id, user_id, name, str(made.id), digest, self.clock())
        return f"<:{name}:{made.id}>"

    async def remove_user(self, user_id: str) -> None:
        """Neplector から外れた人の分を消す。"""
        async with self._lock:
            for row in self.store.avatar_emojis_of(user_id):
                await self._remove(row)

    async def prune(self, members: set[tuple[str, str]]) -> None:
        """サーバーにいなくなった人の分と、keep_days のあいだ表示されていない分を消す。members は (サーバー, 人) の今の名簿。"""
        oldest = self.clock() - self.keep_days * 86400
        async with self._lock:
            for row in self.store.all_avatar_emojis():
                if (row["guild_id"], row["user_id"]) not in members or row["last_used"] < oldest:
                    await self._remove(row)

    async def _remove(self, row: dict) -> None:
        try:
            await self._delete(row["emoji_id"])
        except FAILURES as error:
            self.log(f"アイコンの絵文字を消せませんでした: {type(error).__name__}")
            return
        self.store.drop_avatar_emoji(row["guild_id"], row["user_id"])

    async def _delete(self, emoji_id: str) -> None:
        try:
            emoji = await self.client.fetch_application_emoji(int(emoji_id))
            await emoji.delete()
        except discord.NotFound:
            pass   # もう無い
