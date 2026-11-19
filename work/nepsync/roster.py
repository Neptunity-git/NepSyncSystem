"""名簿（/nepsync-members）と「この人を呼ぶ」の中身（ADR 0014。載せる人・外れる人は ADR 0007）。

- 出すのは、相手のサーバーにだけいて、同期チャンネルを見られる人（判断1）。自分のサーバーにいる人は Discord の入力候補で呼べる
- 並べる順：★ → 最近選んだ人 → このチャンネルで最近発言した人 → そのほかを名前順（判断3）
- 名前で探せる。全角半角・大文字小文字・ひらがなカタカナ・空白の違いを気にしない（判断4）
- 選んだ人の <@ID> は、本文だけに置く（スマホの長押しの「テキストをコピー」で写せる。判断5）。名簿で選ぶと、名簿の発言の本文を書き換える
- 保存するのは ★ と最近選んだ人の ID の組と時刻だけ。発言の中身は使わない

ここは答えの中身を作るだけで、Discord への返事（画像・メニュー・ボタン・フォーム）は bot.py が送る。"""

from __future__ import annotations

import asyncio
import collections
import time
import unicodedata
from typing import Any, Awaitable, Callable

from . import grid
from .avatars import sized_url
from .structure import effective_permissions

PAGE_SIZE = 25            # メニューの選択肢は25個まで（Discord の決まり）
MAX_PICK = 10             # まとめて選べる人数
KEEP_PICKS = 8            # 最近選んだ人を、1人につき何人まで残すか
RECENT_AUTHORS = 10       # 最近発言した人を、何人まで前に出すか
CACHE_SECONDS = 60        # 名簿（人と権限）を手元に置く時間。入力候補は3秒以内に答える決まりなので
AVATAR_CACHE = 200        # アイコンの中身を手元に置く数（並べ直すたびに取り直さないように）
AVATAR_FETCHES = 8        # アイコンを同時に取りに行く数
VIEW, SEND = 1 << 10, 1 << 11
MARK_TEXT = {"star": "★ よく呼ぶ", "picked": "🕘 最近選んだ", "recent": "💬 最近このチャンネルで発言"}


def fold(text: str) -> str:
    """探すための形。全角半角・大文字小文字・カタカナ（→ひらがな）・空白をそろえる。"""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = "".join(chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char for char in text)
    return "".join(text.split())


def matches(people: list[dict], query: str) -> list[dict]:
    """前から当たる人を先に、途中で当たる人をあとに。それぞれの中は、もとの並びのまま。"""
    key = fold(query)
    if not key:
        return list(people)
    front = [person for person in people if any(fold(name).startswith(key) for name in person["names"])]
    inside = [person for person in people if person not in front and any(key in fold(name) for name in person["names"])]
    return front + inside


def arrange(people: list[dict], stars: list[str], picks: list[str], recent: list[str]) -> list[dict]:
    """★ → 最近選んだ → 最近発言した → 名前順。人ごとに mark（star・picked・recent・""）を付けた写しを返す。"""
    rank = {}
    for group, ids in enumerate((stars, picks, recent)):
        for order, user_id in enumerate(ids):
            rank.setdefault(user_id, (group, order))
    marks = ("star", "picked", "recent")
    out = []
    for person in people:
        group, order = rank.get(person["user_id"], (3, 0))
        out.append({**person, "mark": marks[group] if group < 3 else "", "_key": (group, order, fold(person["name"]), person["user_id"])})
    out.sort(key=lambda item: item["_key"])
    for item in out:
        del item["_key"]
    return out


def mentions_text(user_ids: list[str]) -> str:
    return " ".join(f"<@{user_id}>" for user_id in user_ids)


def code_block(user_ids: list[str]) -> str:
    """PC で選んでコピーする形（ADR 0014 判断5）。本文の <@ID> は、PC では名前の印になって、選んでも写せないため。"""
    return f"```\n{mentions_text(user_ids)}\n```"


def copy_embeds(user_ids: list[str]) -> list[dict]:
    """本文の下の枠：選んだ人のコードブロックだけ。誰も選んでいなければ出さない（場所を取らない）。"""
    return [{"description": code_block(user_ids)}] if user_ids else []


def clean_ids(user_ids: list[str]) -> list[str]:
    """数字だけの ID を、重なりなく、MAX_PICK 人まで。"""
    return [item for item in dict.fromkeys(str(user_id) for user_id in user_ids) if item.isdigit()][:MAX_PICK]


def text_only(text: str) -> dict:
    return {"kind": "text", "content": text}


class Roster:
    def __init__(self, config: Any, neplector: Any, client: Any, store: Any, *,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 download: Callable[[str], Awaitable[bytes]] | None = None,
                 render: Callable[[list[dict]], bytes] = grid.render) -> None:
        self.config, self.nep, self.client, self.store = config, neplector, client, store
        self.log, self.clock = log, clock
        self.download = download or getattr(neplector, "download", None)
        self.render = render
        self._people: dict[tuple, tuple[float, list[dict]]] = {}
        self._avatars: collections.OrderedDict[str, bytes | None] = collections.OrderedDict()

    # ---- 誰を出すか ----

    def pair_of_channel(self, channel_id: str) -> tuple[str, str] | None:
        return next(((row["a_id"], row["b_id"]) for row in self.store.channel_pairs() if channel_id in (row["a_id"], row["b_id"])), None)

    def other_side(self, pair: tuple[str, str], guild_id: str) -> tuple[str, str]:
        """相手のサーバーと、そこでの同じチャンネル。"""
        if guild_id == self.config.guild_a:
            return self.config.guild_b, pair[1]
        return self.config.guild_a, pair[0]

    def guild(self, guild_id: str) -> Any:
        found = self.client.get_guild(int(guild_id))
        if found is None or found.unavailable:
            raise LookupError(guild_id)
        return found

    async def people(self, pair: tuple[str, str], guild_id: str, *, cached: bool = False) -> list[dict]:
        """相手のサーバーにだけいて、そこで同期チャンネルを見られる人（名前順）。載せないと決めた人と bot は除く。
        人と持っているロールは Neplector、ロールの権限とチャンネルの上書きは discord.py の手元の控えから。
        cached=True（入力候補）なら、少し前に作ったものを使う。"""
        key = (pair, guild_id)
        kept = self._people.get(key)
        if cached and kept and self.clock() - kept[0] < CACHE_SECONDS:
            return kept[1]
        other_guild, other_channel = self.other_side(pair, guild_id)
        hidden = self.store.hidden_users()
        members = await self.nep.table("members")
        bots = {row["user_id"] for row in members if row.get("is_bot")}
        global_names = {row["user_id"]: row.get("display_name") for row in members}
        here = {row["user_id"] for row in await self.nep.table("member_servers", guild_id=guild_id)}
        held: dict[str, list[str]] = {}
        for row in await self.nep.table("role_periods"):
            if not row.get("ended_at"):
                held.setdefault(row["user_id"], []).append(row["role_id"])
        info = self.guild(other_guild)
        channel = info.get_channel(int(other_channel))
        if channel is None:
            raise LookupError(other_channel)
        owner = str(info.owner_id)
        found = []
        for row in await self.nep.table("member_servers", guild_id=other_guild):
            user_id = row["user_id"]
            if user_id in hidden or user_id in bots or user_id in here:
                continue
            permissions = VIEW | SEND if user_id == owner else effective_permissions(other_guild, user_id, held.get(user_id, []), info.roles, channel.overwrites)
            if not permissions & VIEW:
                continue
            name = row.get("display_name") or "メンバー"
            names = [name] + [item for item in (global_names.get(user_id),) if item and item != name]
            found.append({"user_id": user_id, "name": name, "names": names, "avatar": row.get("avatar_url"),
                          "can_send": bool(permissions & SEND)})
        found.sort(key=lambda item: (fold(item["name"]), item["user_id"]))
        self._people[key] = (self.clock(), found)
        return found

    async def arranged(self, channel_id: str, guild_id: str, user_id: str, *, cached: bool = False) -> tuple[tuple[str, str] | None, list[dict]]:
        pair = self.pair_of_channel(channel_id)
        if pair is None:
            return None, []
        people = await self.people(pair, guild_id, cached=cached)
        recent = self.store.recent_authors(channel_id, RECENT_AUTHORS)
        return pair, arrange(people, self.store.stars(user_id), self.store.picks(user_id), recent)

    # ---- 名簿のページ ----

    async def page(self, channel_id: str, page: int, guild_id: str, user_id: str, *, query: str | None = None,
                   selected: list[str] | None = None) -> dict:
        """答えは {"kind": "page", "content"（選んだ人の <@ID> だけ）, "embeds"（説明）, "image"（PNG）, "options"（メニュー）,
        "pager", "page", "pages", "selected", "star"} か {"kind": "text", "content"}。
        selected は、前に選んでいた人（ページを送っても残す。ADR 0014 判断4）。"""
        selected = clean_ids(selected or [])
        pair, people = await self.arranged(channel_id, guild_id, user_id)
        if pair is None:
            return text_only("同期しているチャンネルで実行してください")
        other_name = self.guild(self.other_side(pair, guild_id)[0]).name
        if query:
            people = matches(people, query)[:PAGE_SIZE]
            if not people:
                return text_only(f"「{query}」に当たる人が、{other_name} にいません")
        if not people:
            return text_only(f"{other_name} にだけいて、このチャンネルを見られる人はいません")
        pages = (len(people) + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(max(page, 0), pages - 1)
        start = page * PAGE_SIZE
        shown = people[start:start + PAGE_SIZE]
        cells = [{"number": start + index + 1, "name": person["name"], "mark": person["mark"]} for index, person in enumerate(shown)]
        pictures = await self._pictures([person["avatar"] for person in shown])
        for cell, picture in zip(cells, pictures):
            cell["avatar"] = picture
        image = await asyncio.to_thread(self.render, cells)
        options = [{"label": f"{start + index + 1}. {person['name']}"[:100], "value": person["user_id"],
                    "description": MARK_TEXT.get(person["mark"]) or ("発言できる" if person["can_send"] else "見るだけ")}
                   for index, person in enumerate(shown)]
        pager = {"channel_id": channel_id, "page": page, "pages": pages} if pages > 1 and not query else None
        return {"kind": "page", "content": mentions_text(selected), "embeds": copy_embeds(selected), "image": image,
                "options": options, "pager": pager, "page": page, "pages": pages if pager else 1, "selected": selected,
                "star": self.starred(user_id, selected[0]) if len(selected) == 1 else None}

    async def _pictures(self, urls: list[str | None]) -> list[bytes | None]:
        gate = asyncio.Semaphore(AVATAR_FETCHES)

        async def one(url: str | None) -> bytes | None:
            if not url or self.download is None:
                return None
            sized = sized_url(url)
            if sized in self._avatars:
                self._avatars.move_to_end(sized)
                return self._avatars[sized]
            async with gate:
                try:
                    data: bytes | None = await self.download(sized)
                except Exception:  # noqa: BLE001（取れなければ、名前の1文字目で代える）
                    data = None
            self._avatars[sized] = data
            while len(self._avatars) > AVATAR_CACHE:
                self._avatars.popitem(last=False)
            return data

        return list(await asyncio.gather(*(one(url) for url in urls)))

    # ---- 探す ----

    async def suggest(self, channel_id: str, guild_id: str, user_id: str, query: str) -> list[tuple[str, str]]:
        """入力候補（名前, 人の ID）。25人まで。"""
        pair, people = await self.arranged(channel_id, guild_id, user_id, cached=True)
        if pair is None:
            return []
        out = []
        for person in matches(people, query)[:PAGE_SIZE]:
            sign = {"star": "★ ", "picked": "🕘 ", "recent": "💬 "}.get(person["mark"], "")
            out.append((f"{sign}{person['name']}"[:100], person["user_id"]))
        return out

    async def search(self, channel_id: str, guild_id: str, user_id: str, query: str) -> dict:
        """名前の欄つきで打たれた。候補から選んだ（人の ID）か、1人だけ当たれば「呼ぶ」画面、何人も当たれば、その人たちの格子。"""
        pair, people = await self.arranged(channel_id, guild_id, user_id)
        if pair is None:
            return text_only("同期しているチャンネルで実行してください")
        chosen = [person for person in people if person["user_id"] == query.strip()]
        found = chosen or matches(people, query)
        if len(found) == 1:
            return await self.pick(guild_id, user_id, [found[0]["user_id"]])
        return await self.page(channel_id, 0, guild_id, user_id, query=query)

    # ---- 選んだあと ----

    async def pick(self, guild_id: str, user_id: str, targets: list[str], *, remember: bool = True) -> dict:
        """選んだ人の「呼ぶ」画面。本文は <@ID> だけ（スマホの「テキストをコピー」で、そのまま写せるように）。説明と名前は埋め込みに。"""
        targets = clean_ids(targets)
        if not targets:
            return text_only("呼ぶ人を選んでください")
        if remember:
            self.remember(user_id, targets)
        names, avatar = [], None
        for target in targets:
            rows = await self.nep.table("member_servers", user_id=target)
            row = next((item for item in rows if item["guild_id"] != guild_id), rows[0] if rows else {})
            names.append(row.get("display_name") or "メンバー")
            avatar = avatar or row.get("avatar_url")
        embed: dict[str, Any] = {"title": "・".join(names)[:256], "description": code_block(targets)}
        if avatar and len(targets) == 1:
            embed["thumbnail"] = {"url": avatar}
        star = self.starred(user_id, targets[0]) if len(targets) == 1 else None
        return {"kind": "pick", "content": mentions_text(targets), "embeds": [embed], "targets": targets, "selected": targets, "star": star}

    def remember(self, user_id: str, targets: list[str]) -> None:
        """選んだ人を「最近選んだ人」に足す。覚えられなくても、呼ぶ文字は出す。"""
        targets = clean_ids(targets)
        if not targets:
            return
        try:
            self.store.add_picks(user_id, targets, self.clock(), KEEP_PICKS)
        except Exception as error:  # noqa: BLE001
            self.log(f"選んだ人を覚えられませんでした: {type(error).__name__}")

    def starred(self, user_id: str, target_id: str) -> bool:
        return str(target_id) in self.store.stars(user_id)

    def set_star(self, user_id: str, target_id: str, on: bool) -> bool:
        """★ に入れる・外す。保存できたか。"""
        if not str(target_id).isdigit():
            return False
        try:
            self.store.set_star(user_id, str(target_id), on, self.clock())
            return True
        except Exception as error:  # noqa: BLE001
            self.log(f"★ を保存できませんでした: {type(error).__name__}")
            return False

    async def call_author(self, message_id: str, guild_id: str, user_id: str) -> dict:
        """写しの長押しのメニュー「この人を呼ぶ」（判断6）。写しを書いた人（元の発言の人）を選んだことにする。"""
        pair = self.store.message_by_copy(message_id)
        if pair is None:
            return text_only("相手のサーバーから来た写しにだけ使えます")
        if pair["author_id"] == user_id:
            return text_only("これはあなたの発言の写しです")
        return await self.pick(guild_id, user_id, [pair["author_id"]])
