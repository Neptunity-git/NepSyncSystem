"""テスト用の偽物。データはすべて架空。

Discord 側は discord.py の形（Client・Guild・チャンネル・Webhook）をまねる。中身は辞書で持ち、テストから直接見たり変えたりできる
（discord.channels["111"]["name"] = ... は、Discord で名前が変わって、その知らせで控えが変わったのと同じ）。
偽物の関数は、呼ばれるたびに本物の discord.py の関数と引数の形を照らし合わせる（名前の書き間違いや、本物に無い引数を見つける）。"""

from __future__ import annotations

import functools
import inspect
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import discord

from nepsync.config import Config
from nepsync.engine import Engine
from nepsync.store import Store

GUILD_A, GUILD_B = "100", "200"
CAT_A, CAT_B = "110", "210"
BOT_ID = "999"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
THREAD = 11
EDITABLE = {"name", "topic", "slowmode_delay", "nsfw", "available_tags", "reason"}   # 偽物が受ける channel.edit の値（本物は **options）


def http_error(kind: type, status: int, code: int, message: str) -> discord.HTTPException:
    return kind(SimpleNamespace(status=status, reason=message), {"code": code, "message": message})


def not_found(code: int = 10003, message: str = "Unknown Channel") -> discord.HTTPException:
    return http_error(discord.NotFound, 404, code, message)


def forbidden(code: int = 50001, message: str = "Missing Access") -> discord.HTTPException:
    return http_error(discord.Forbidden, 403, code, message)


def mirrors(real: Any):
    """本物の discord.py の関数と同じ形で呼ばれているかを、呼ばれるたびに確かめる。"""
    signature = inspect.signature(real)

    def wrap(fake):
        @functools.wraps(fake)
        def checked(self, *args, **kwargs):
            signature.bind(self, *args, **kwargs)
            return fake(self, *args, **kwargs)
        return checked
    return wrap


def _mentions(allowed: discord.AllowedMentions) -> dict:
    result = allowed.to_dict()
    for key in ("users", "roles"):
        if key in result:
            result[key] = [str(item) for item in result[key]]
    return result


def _tag(item: dict) -> discord.ForumTag:
    tag = discord.ForumTag(name=item["name"], moderated=bool(item.get("moderated")), emoji=item.get("emoji_name") or None)
    tag.id = int(item["id"])
    return tag


class FakeDiscord:
    """偽物の Discord。client が discord.py の Client の代わり。"""

    def __init__(self) -> None:
        self.channels: dict[str, dict] = {}
        self.webhooks: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.writes: list[tuple] = []      # 変更の記録。どのサーバーに書いたかを確かめる
        self.forbidden: set[str] = set()   # 403 を返すチャンネル
        self.reactions: dict[str, set[str]] = {}   # bot が付けたリアクション
        self.pins: set[str] = set()   # ピン留めされた発言
        self.owners: dict[str, str] = {}
        self.roles: dict[str, list[dict]] = {}
        self.app_emojis: dict[str, dict] = {}
        self.unavailable: set[str] = set()   # 控えがまだ無いサーバー
        self.lagging = False                  # True なら、並べ替えを送っても控え（位置）が変わらない（知らせが届かない）
        self.failures: dict[str, list[Exception]] = {}
        self._next = 1000
        self.client = FakeClient(self)

    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    def fail(self, name: str, *errors: Exception | None) -> None:
        """name の操作が次に呼ばれたとき、errors を順に投げる（None はその回は成功）。"""
        self.failures.setdefault(name, []).extend(errors)

    def _maybe_fail(self, name: str) -> None:
        queue = self.failures.get(name)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    def add(self, channel_id: str, guild_id: str, type_: int, name: str, parent: str | None = None, **extra) -> dict:
        self.channels[channel_id] = {"id": channel_id, "guild_id": guild_id, "type": type_, "name": name, "parent_id": parent, "position": int(channel_id) % 100, **extra}
        return self.channels[channel_id]

    def guild_of_channel(self, channel_id: str) -> str | None:
        item = self.channels.get(channel_id)
        return item["guild_id"] if item else None

    def partial_webhook(self, webhook_id: int, token: str) -> "FakeWebhook":
        return FakeWebhook(self, str(webhook_id), token)


class FakeClient:
    def __init__(self, world: FakeDiscord) -> None:
        self.world = world
        self.user = SimpleNamespace(id=int(BOT_ID))
        self.application_id = int(BOT_ID)

    @mirrors(discord.Client.get_guild)
    def get_guild(self, id, /):
        return FakeGuild(self.world, str(id))

    @mirrors(discord.Client.get_channel)
    def get_channel(self, id, /):
        return FakeChannel(self.world, str(id)) if str(id) in self.world.channels else None

    @mirrors(discord.Client.fetch_channel)
    async def fetch_channel(self, channel_id, /):
        key = str(channel_id)
        if key in self.world.forbidden:
            raise forbidden()
        if key not in self.world.channels:
            raise not_found()
        return FakeChannel(self.world, key)

    @mirrors(discord.Client.get_partial_messageable)
    def get_partial_messageable(self, id, *, guild_id=None, type=None):
        return SimpleNamespace(id=id, get_partial_message=lambda message_id: FakePartialMessage(self.world, str(id), str(message_id)))

    @mirrors(discord.Client.create_application_emoji)
    async def create_application_emoji(self, *, name, image):
        self.world._maybe_fail("create_application_emoji")
        if any(item["name"] == name for item in self.world.app_emojis.values()):
            raise http_error(discord.HTTPException, 400, 50035, "Invalid Form Body")
        item = {"id": self.world._id(), "name": name, "image": image}
        self.world.app_emojis[item["id"]] = item
        self.world.writes.append(("create_app_emoji", name))
        return FakeEmoji(self.world, item["id"])

    @mirrors(discord.Client.fetch_application_emojis)
    async def fetch_application_emojis(self):
        return [FakeEmoji(self.world, key) for key in self.world.app_emojis]

    @mirrors(discord.Client.fetch_application_emoji)
    async def fetch_application_emoji(self, emoji_id, /):
        if str(emoji_id) not in self.world.app_emojis:
            raise not_found(10014, "Unknown Emoji")
        return FakeEmoji(self.world, str(emoji_id))


class FakeEmoji:
    def __init__(self, world: FakeDiscord, emoji_id: str) -> None:
        self.world, self.id, self.name = world, int(emoji_id), world.app_emojis[emoji_id]["name"]

    @mirrors(discord.Emoji.delete)
    async def delete(self, *, reason=None):
        if str(self.id) not in self.world.app_emojis:
            raise not_found(10014, "Unknown Emoji")
        del self.world.app_emojis[str(self.id)]
        self.world.writes.append(("delete_app_emoji", str(self.id)))


class FakeGuild:
    def __init__(self, world: FakeDiscord, guild_id: str) -> None:
        self.world, self.key, self.id = world, guild_id, int(guild_id)

    @property
    def name(self) -> str:
        return f"サーバー{self.key}"

    @property
    def owner_id(self) -> int | None:
        owner = self.world.owners.get(self.key)
        return int(owner) if owner else None

    @property
    def unavailable(self) -> bool:
        return self.key in self.world.unavailable

    @property
    def channels(self) -> list:
        return [FakeChannel(self.world, key) for key, item in self.world.channels.items() if item["guild_id"] == self.key and item["type"] != THREAD]

    @property
    def roles(self) -> list:
        rows = self.world.roles.get(self.key, [{"id": self.key, "permissions": str((1 << 10) | (1 << 11))}])
        return [SimpleNamespace(id=int(row["id"]), name=row.get("name", ""), permissions=discord.Permissions(int(row["permissions"])),
                                mentionable=bool(row.get("mentionable"))) for row in rows]

    def get_role(self, role_id: int):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_channel(self, channel_id: int):
        item = self.world.channels.get(str(channel_id))
        return FakeChannel(self.world, str(channel_id)) if item and item["guild_id"] == self.key and item["type"] != THREAD else None

    def _created(self, name: str, type_: int, category: Any, topic: Any, slowmode_delay: Any, nsfw: Any, tags: list | None = None) -> "FakeChannel":
        self.world._maybe_fail("create_channel")
        channel_id = self.world._id()
        self.world.writes.append(("create_channel", self.key, name))
        extra: dict[str, Any] = {"topic": topic if isinstance(topic, str) else None}
        if isinstance(slowmode_delay, int):
            extra["rate_limit_per_user"] = slowmode_delay
        if nsfw is True:
            extra["nsfw"] = True
        if tags is not None:
            extra["available_tags"] = [{"id": self.world._id(), **_tag_dict(tag)} for tag in tags]
        self.world.add(channel_id, self.key, type_, name, str(category.id) if category is not None else None, **extra)
        return FakeChannel(self.world, channel_id)

    @mirrors(discord.Guild.create_text_channel)
    async def create_text_channel(self, name, *, category=None, topic=None, slowmode_delay=None, nsfw=None, **_):
        return self._created(name, 0, category, topic, slowmode_delay, nsfw)

    @mirrors(discord.Guild.create_forum)
    async def create_forum(self, name, *, category=None, topic=None, slowmode_delay=None, nsfw=None, available_tags=(), **_):
        return self._created(name, 15, category, topic, slowmode_delay, nsfw, list(available_tags))


def _tag_dict(tag: discord.ForumTag) -> dict:
    emoji = tag.emoji.name if tag.emoji is not None and not tag.emoji.id else None
    return {"name": tag.name, "moderated": tag.moderated, "emoji_name": emoji}


class FakeChannel:
    """チャンネルかスレッド（type 11）。中身は world.channels の辞書を見る。"""

    def __init__(self, world: FakeDiscord, channel_id: str) -> None:
        self.world, self.key, self.id = world, channel_id, int(channel_id)

    @property
    def data(self) -> dict:
        return self.world.channels[self.key]

    @property
    def guild(self) -> FakeGuild:
        return FakeGuild(self.world, self.data["guild_id"])

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def type(self) -> discord.ChannelType:
        return discord.ChannelType(self.data["type"])

    @property
    def position(self) -> int:
        return self.data.get("position", 0)

    @property
    def topic(self) -> str | None:
        return self.data.get("topic")

    @property
    def slowmode_delay(self) -> int:
        return self.data.get("rate_limit_per_user") or 0

    @property
    def nsfw(self) -> bool:
        return bool(self.data.get("nsfw"))

    @property
    def category_id(self) -> int | None:
        parent = self.data.get("parent_id")
        return int(parent) if parent and self.data["type"] != THREAD else None

    @property
    def parent_id(self) -> int | None:
        parent = self.data.get("parent_id")
        return int(parent) if parent else None

    @property
    def available_tags(self) -> list:
        return [_tag(item) for item in self.data.get("available_tags") or []]

    @property
    def overwrites(self) -> dict:
        return {discord.Object(id=int(item["id"])): discord.PermissionOverwrite.from_pair(discord.Permissions(int(item["allow"])), discord.Permissions(int(item["deny"])))
                for item in self.data.get("permission_overwrites") or []}

    def _record_edit(self, body: dict) -> None:
        self.world.writes.append(("modify_channel", self.data["guild_id"], self.key, body))
        self.data.update({key: value for key, value in body.items() if key != "available_tags"})
        if "available_tags" in body:
            self.data["available_tags"] = [{**tag, "id": tag.get("id") or self.world._id()} for tag in body["available_tags"]]

    async def edit(self, **options):
        """チャンネルは channel.edit（本物は **options なので、受ける名前を EDITABLE で確かめる）、スレッドは Thread.edit。"""
        if self.data["type"] == THREAD:
            inspect.signature(discord.Thread.edit).bind(self, **options)
        else:
            unknown = set(options) - EDITABLE
            assert not unknown, f"channel.edit に渡す名前が違う: {unknown}"
        self.world._maybe_fail("edit")
        names = {"slowmode_delay": "rate_limit_per_user"}
        body: dict[str, Any] = {}
        for key, value in options.items():
            if key == "available_tags":
                body[key] = [{**_tag_dict(tag), **({"id": str(tag.id)} if tag.id else {})} for tag in value]
            elif key == "applied_tags":
                body[key] = [str(tag.id) for tag in value]
            elif key != "reason":
                body[names.get(key, key)] = value
        self._record_edit(body)
        return self

    @mirrors(discord.abc.GuildChannel.delete)
    async def delete(self, *, reason=None):
        self.world.writes.append(("delete_channel", self.world.guild_of_channel(self.key), self.key))
        if self.key not in self.world.channels:
            raise not_found()
        del self.world.channels[self.key]

    async def move(self, *, before):
        """discord.py の move と同じく、同じカテゴリの文字のチャンネルを 0 から並べ直す。"""
        inspect.signature(discord.abc.GuildChannel.move).bind(self, before=before)
        self.world._maybe_fail("move")
        guild = self.data["guild_id"]
        siblings = [item for item in self.world.channels.values()
                    if item["guild_id"] == guild and item.get("parent_id") == self.data.get("parent_id") and item["type"] in (0, 5, 15, 16)]
        order = [item["id"] for item in sorted(siblings, key=lambda item: (item.get("position", 0), int(item["id"])))]
        order.remove(self.key)
        order.insert(order.index(str(before.id)), self.key)
        body = [{"id": key, "position": index} for index, key in enumerate(order)]
        self.world.writes.append(("reorder", guild, body))
        for item in body if not self.world.lagging else []:
            self.world.channels[item["id"]]["position"] = item["position"]

    @mirrors(discord.TextChannel.webhooks)
    async def webhooks(self):
        return [SimpleNamespace(id=int(hook["id"]), token=hook["token"], user=SimpleNamespace(id=int(hook["user"]["id"])))
                for hook in self.world.webhooks.values() if hook["channel_id"] == self.key]

    @mirrors(discord.TextChannel.create_webhook)
    async def create_webhook(self, *, name, avatar=None, reason=None):
        hook = {"id": self.world._id(), "token": "fake-token", "channel_id": self.key, "user": {"id": BOT_ID}, "name": name}
        self.world.writes.append(("create_webhook", self.data["guild_id"], self.key))
        self.world.webhooks[hook["id"]] = hook
        return SimpleNamespace(id=int(hook["id"]), token=hook["token"])

    @mirrors(discord.TextChannel.create_thread)
    async def create_thread(self, *, name, message=None, auto_archive_duration=10080, type=None, reason=None, invitable=True, slowmode_delay=None):
        thread_id = self.world._id()
        self.world.writes.append(("create_thread", self.data["guild_id"], name))
        self.world.add(thread_id, self.data["guild_id"], THREAD, name, self.key)
        return FakeChannel(self.world, thread_id)


class FakePartialMessage:
    def __init__(self, world: FakeDiscord, channel_id: str, message_id: str) -> None:
        self.world, self.channel_id, self.message_id = world, channel_id, message_id

    @mirrors(discord.PartialMessage.add_reaction)
    async def add_reaction(self, emoji, /):
        self.world._maybe_fail("add_reaction")
        self.world.writes.append(("add_reaction", self.world.guild_of_channel(self.channel_id), self.message_id, emoji))
        self.world.reactions.setdefault(self.message_id, set()).add(emoji)

    @mirrors(discord.PartialMessage.pin)
    async def pin(self, *, reason=None):
        self.world._maybe_fail("pin")
        self.world.writes.append(("pin", self.world.guild_of_channel(self.channel_id), self.channel_id, self.message_id))
        self.world.pins.add(self.message_id)

    @mirrors(discord.PartialMessage.unpin)
    async def unpin(self, *, reason=None):
        self.world._maybe_fail("unpin")
        self.world.writes.append(("unpin", self.world.guild_of_channel(self.channel_id), self.channel_id, self.message_id))
        self.world.pins.discard(self.message_id)

    @mirrors(discord.PartialMessage.remove_reaction)
    async def remove_reaction(self, emoji, member):
        assert member.id == int(BOT_ID), "bot 以外のリアクションは外さない"
        self.world.writes.append(("remove_reaction", self.world.guild_of_channel(self.channel_id), self.message_id, emoji))
        self.world.reactions.get(self.message_id, set()).discard(emoji)


class FakeWebhook:
    def __init__(self, world: FakeDiscord, webhook_id: str, token: str) -> None:
        self.world, self.key, self.id, self.token = world, webhook_id, int(webhook_id), token

    @mirrors(discord.Webhook.send)
    async def send(self, content=None, *, username=None, avatar_url=None, allowed_mentions=None, files=(), thread=None, thread_name=None,
                   wait=False, suppress_embeds=False, silent=False, applied_tags=(), poll=None, **_):
        assert wait, "写しの ID を残すので wait=True で送る"
        self.world._maybe_fail("send")
        if self.key not in self.world.webhooks:
            raise not_found(10015, "Unknown Webhook")
        channel_id = self.world.webhooks[self.key]["channel_id"]
        thread_id = str(thread.id) if thread is not None else None
        if thread_id and thread_id not in self.world.channels:
            raise not_found()
        place = thread_id or channel_id
        payload: dict[str, Any] = {"content": content, "username": username, "allowed_mentions": _mentions(allowed_mentions)}
        if avatar_url:
            payload["avatar_url"] = avatar_url
        if poll is not None:
            made = poll._to_dict()
            made.pop("layout_type", None)
            payload["poll"] = made
        flags = (4 if suppress_embeds else 0) | (4096 if silent else 0)
        if flags:
            payload["flags"] = flags
        if thread_name:
            payload["thread_name"] = thread_name
            payload["applied_tags"] = [str(tag.id) for tag in applied_tags]
            place = self.world.add(self.world._id(), self.world.channels[channel_id]["guild_id"], THREAD, thread_name, channel_id, applied_tags=payload["applied_tags"])["id"]
        message = {"id": self.world._id(), "channel_id": place, "webhook_id": self.key, **payload,
                   "files": [{"filename": item.filename, "data": item.fp.read()} for item in files]}
        self.world.messages[message["id"]] = message
        self.world.writes.append(("post", self.world.guild_of_channel(channel_id), place))
        return SimpleNamespace(id=int(message["id"]), channel=SimpleNamespace(id=int(place)))

    @mirrors(discord.Webhook.edit_message)
    async def edit_message(self, message_id, *, content=None, allowed_mentions=None, thread=None, **_):
        if self.key not in self.world.webhooks:
            raise not_found(10015, "Unknown Webhook")
        if str(message_id) not in self.world.messages:
            raise not_found(10008, "Unknown Message")
        self.world.writes.append(("edit", self.key, str(message_id)))
        self.world.messages[str(message_id)].update(content=content, allowed_mentions=_mentions(allowed_mentions))

    @mirrors(discord.Webhook.delete_message)
    async def delete_message(self, message_id, /, *, thread=None):
        if self.key not in self.world.webhooks:
            raise not_found(10015, "Unknown Webhook")
        self.world.writes.append(("delete_message", self.key, str(message_id)))
        self.world.messages.pop(str(message_id), None)


class FakeNeplector:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.bodies: dict[str, dict] = {}
        self.tables: dict[str, list[dict]] = {"channels": [], "member_servers": [], "threads": [], "roles": [], "emojis": [], "role_periods": [], "members": []}
        self.forget_rows: list[dict] = []
        self.requested: list[str] = []

    async def latest_seq(self) -> int:
        return self.last_seq()

    def last_seq(self) -> int:
        return self.rows[-1]["seq"] if self.rows else 0

    async def changes(self, after: int, limit: int = 500, wait: int = 25) -> dict:
        rows = [row for row in self.rows if row["seq"] > after][:limit]
        return {"changes": rows, "next_after": rows[-1]["seq"] if rows else after, "first_seq": 1, "last_seq": self.last_seq()}

    async def messages(self, ids: list[str]) -> dict[str, dict]:
        self.requested.extend(ids)
        return {item: self.bodies[item] for item in ids if item in self.bodies}

    async def forget(self, after: int, limit: int = 500) -> dict:
        rows = [row for row in self.forget_rows if row["seq"] > after][:limit]
        return {"forget": rows, "next_after": rows[-1]["seq"] if rows else after}

    async def table(self, name: str, /, **filters: str) -> list[dict]:
        return [row for row in self.tables.get(name, []) if all(row.get(key) == value for key, value in filters.items())]

    async def close(self) -> None:
        pass

    # テストを書きやすくする部品
    def event(self, kind: str, **fields) -> dict:
        row = {"seq": self.last_seq() + 1, "kind": kind, **fields}
        self.rows.append(row)
        return row

    def say(self, message_id: str, channel_id: str, guild_id: str, content: str = "こんにちは", author: str = "501", counted: int = 1, thread_id: str | None = None, **extra) -> dict:
        self.bodies[message_id] = {"message_id": message_id, "channel_id": channel_id, "thread_id": thread_id or channel_id, "guild_id": guild_id, "author_id": author,
                                   "created_at": "2026-11-09T11:59:30+00:00", "edited_at": None, "content": content, "counted": counted, "flags": 0, "webhook_id": None,
                                   "reply_to_id": None, "attachments": [], "stickers": [], "forward": None, "poll": None, "reactions": [], **extra}
        return self.event("message.create", message_id=message_id, channel_id=channel_id, thread_id=thread_id or channel_id, user_id=author, counted=counted)


def memory_store() -> Store:
    """メモリの中の SQLite。テストで要らなくなったら閉じる。"""
    store = Store(":memory:")
    weakref.finalize(store, store.db.close)
    return store


async def fake_download(url: str) -> bytes:
    return b"fake-bytes"


def make_world(direction: str = "a_to_b", **options):
    """A に架空のカテゴリとテキストチャンネル、B に空のカテゴリがある世界。"""
    discord_, nep = FakeDiscord(), FakeNeplector()
    discord_.add(CAT_A, GUILD_A, 4, "共同")
    discord_.add(CAT_B, GUILD_B, 4, "共同")
    discord_.add("111", GUILD_A, 0, "雑談", CAT_A, topic="架空のトピック")
    nep.tables["channels"].append({"channel_id": "111", "category_id": CAT_A, "kind": "text", "name": "雑談", "reading": 1, "guild_id": GUILD_A})
    nep.tables["member_servers"].append({"guild_id": GUILD_A, "user_id": "501", "display_name": "架空さん", "avatar_url": "https://example.invalid/a.png"})
    nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": "502", "display_name": "相手さん", "avatar_url": None})
    config = Config(guild_a=GUILD_A, guild_b=GUILD_B, category_names=("共同",), direction=direction, **options)
    notices: list[str] = []
    engine = Engine(config, nep, discord_.client, memory_store(), notify=notices.append, download=fake_download, now=lambda: NOW,
                    log=lambda text: None, partial_webhook=discord_.partial_webhook)
    return engine, nep, discord_, notices


async def started(direction: str = "a_to_b", **options):
    """構造をそろえて、読む位置を今にした世界。"""
    engine, nep, discord_, notices = make_world(direction, **options)
    await engine.sync_structure()
    engine.store.set("changes_after", nep.last_seq())
    discord_.writes.clear()
    return engine, nep, discord_, notices


async def run(engine: Engine) -> None:
    """読む位置より後の変更を、まとめて処理する。"""
    await engine.handle_batch([row for row in engine.nep.rows if row["seq"] > int(engine.store.get("changes_after") or 0)])


def copy_of(engine: Engine, channel_id: str) -> str:
    place = engine.store.channel_side(channel_id)
    assert place is not None, channel_id
    return place[1]
