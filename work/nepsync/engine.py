"""同期の本体。Neplector の変更を読み、discord.py で Discord に反映する。1本の流れで順に処理する（同時に動かさない）。

- 人の情報（発言・リアクション・票・名簿・忘れる知らせ）は Neplector から読む
- チャンネル・カテゴリ・ロールの形は discord.py の手元の控え（GUILDS の知らせで保たれる）から読む（ADR 0010 判断2）
- 書くのは discord.py の関数（Webhook.send、channel.edit、guild.create_text_channel など）

守ること：本文を保存しない・ログに書かない。チャンネルを消すのは channel.delete のときだけ、
Discord に直接聞いた 404 の確認と上限のあとで（手元の控えは、消えたことの確認に使わない）。"""

from __future__ import annotations

import asyncio
import io
import json
import math
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiohttp
import discord

from . import rewrite, structure
from .config import Config
from .neplector import NeplectorError, Transient, why
from .store import Store, other

WEBHOOK_NAME = "NepSync"
NO_PINGS = discord.AllowedMentions.none()
SUPPRESS_EMBEDS, SILENT = 4, 4096   # 写す発言の印：リンクの展開を隠す・サイレント
CACHE_SECONDS = 600
STRUCTURE_KINDS = {"channel.create", "channel.update", "channel.stop"}
REACTION_KINDS = {"reaction.add", "reaction.remove", "reaction.clear"}
POLL_VOTE_KINDS = {"poll.vote.add", "poll.vote.remove"}
STALL_NOTICE_SECONDS = 900    # 一時的な失敗がこれだけ続いたら、運営者に1回知らせる（Neplector の移行などで API に届かないとき）
TALLY_SECONDS = 10           # 合計の発言を書き換えるのは、1つの投票につき最大で10秒に1回（ADR 0011 判断1）
MAX_POLL_HOURS = 768          # Discord の上限（32日）
CURSOR, FORGET_CURSOR = "changes_after", "forget_after"
SHARE, WITHDRAW = "share", "withdraw"     # 告知の依頼（ADR 0009）
ANNOUNCE_WAIT_SECONDS = 600   # Neplector に記録が届くのを待つ長さ。過ぎたら「記録が無い」と答える
ANNOUNCE_KEEP_SECONDS = 86400 # 終わった依頼を消すまでの長さ
SETTLE_SECONDS = 10.0         # 並べ替えのあと、手元の控えが知らせで追いつくのを待つ長さ
UNKNOWN_CHANNEL, UNKNOWN_MESSAGE, UNKNOWN_EMOJI, UNKNOWN_WEBHOOK, TOO_LARGE = 10003, 10008, 10014, 10015, 40005
TOO_MANY_PINS = 30003         # 1つのチャンネルのピン留めは250件まで
TEXT_BUCKET = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum, discord.ChannelType.media)
FAILED = (discord.HTTPException, NeplectorError)   # やり直しても直らない答え（ただし DiscordServerError は一時的な失敗）
TRANSIENT = (Transient, discord.DiscordServerError, discord.GatewayNotFound, aiohttp.ClientError, OSError, asyncio.TimeoutError)


class NotReady(Exception):
    """サーバーの控えがまだ無い（つないだ直後や、Discord の障害でサーバーが見えないとき）。構造には触らず、次の回にやる。"""


def describe(error: Exception) -> str:
    return f"{getattr(error, 'status', '?')} code={getattr(error, 'code', None)}"


class Engine:
    def __init__(self, config: Config, neplector: Any, client: Any, store: Store, *,
                 notify: Callable[[str], None] = print, download: Callable[[str], Awaitable[bytes]] | None = None,
                 now: Callable[[], datetime] | None = None, log: Callable[[str], None] = print,
                 partial_webhook: Callable[[int, str], Any] | None = None) -> None:
        self.config, self.nep, self.client, self.store = config, neplector, client, store
        self.notify, self.download, self.log = notify, download, log
        self.now = now or (lambda: datetime.now(timezone.utc))
        # 保存した ID と token から、discord.py の Webhook を作る（bot の接続を使う）
        self.partial_webhook = partial_webhook or (lambda webhook_id, token: discord.Webhook.partial(webhook_id, token, client=client))
        self.lock = asyncio.Lock()   # 同期の本体と、コマンドからの告知の依頼を、同時に動かさない
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._last_structure = 0.0
        self._last_forget = 0.0
        self._names_cleared = False
        self.avatars: Any = None   # アイコンの絵文字（ADR 0004 判断8）。bot が入れる
        self._reactions_done: set[str] = set()

    # ------------------------------------------------------------ 読む位置

    async def start_cursor(self) -> int:
        """初めて動かすときは今の番号から。過去の発言は流さない。"""
        saved = self.store.get(CURSOR)
        if saved is not None:
            return int(saved)
        latest = await self.nep.latest_seq()
        self.store.set(CURSOR, latest)
        return latest

    async def run_once(self, wait: int = 25) -> None:
        after = await self.start_cursor()
        async with self.lock:
            await self.periodic()
        body = await self.nep.changes(after, wait=self._tally_wait(wait))   # 長く待つので、鍵は持たない
        first = body.get("first_seq") or 0
        if first and after < first - 1:
            self.notify(f"変更の記録に読めなかった部分があります（{after} の次が {first}）。止まっていた時間が30日を超えた可能性があります")
        async with self.lock:
            await self.handle_batch(body.get("changes", []))
            self.store.set(CURSOR, max(body.get("next_after", after), self._cursor()))

    def _tally_wait(self, wait: int) -> int:
        """書き換え待ちの投票の合計があれば、書き換えてよい時刻までしか Neplector を待たない（待つあいだは書き換えないので）。"""
        if not self.config.sync_polls:
            return wait
        stamp = self.now().timestamp()
        due = [row["sent_at"] + TALLY_SECONDS - stamp for row in self.store.open_tallies() if row["dirty"]]
        return max(1, min(wait, math.ceil(min(due)))) if due else wait

    def _cursor(self) -> int:
        return int(self.store.get(CURSOR) or 0)

    async def periodic(self) -> None:
        clock = self.now().timestamp()
        if not self._names_cleared:
            await self._keep_going("写しの末尾の名前の片付け", self.clear_reactor_names)
        if self.config.sync_structure and clock - self._last_structure >= self.config.structure_interval_seconds:
            self._last_structure = clock
            await self._keep_going("構造の見回り", self.sync_structure)
        if clock - self._last_forget >= self.config.forget_interval_seconds:
            self._last_forget = clock
            await self._keep_going("忘れる処理", self.process_forget)
        if self.config.sync_polls:
            await self._keep_going("投票の合計の更新", self.flush_tallies)
        if self.config.announcements:
            await self.process_announcements()

    async def _keep_going(self, label: str, work: Callable[[], Awaitable[Any]]) -> None:
        """権限の問題などで、発言の同期まで止めない。次の回にまたやる。"""
        try:
            await work()
        except discord.DiscordServerError:
            raise
        except FAILED as error:
            self.notify(f"{label}ができませんでした: {describe(error)}。次の回にやり直します")
        except NotReady as error:
            self.log(f"{label}は後にします（{error}）")

    # ------------------------------------------------------------ 変更の振り分け

    async def handle_batch(self, rows: list[dict]) -> None:
        if self.config.sync_structure and any(row.get("kind") in STRUCTURE_KINDS for row in rows):
            await self._keep_going("構造の同期", self.sync_structure)
        messages = await self._prefetch(rows)
        self._reactions_done = set()
        for row in rows:
            if row.get("kind") == "reaction.clear":   # 誰かがまとめて消すと、bot の分も消える
                self.store.clear_bot_reactions(row["message_id"], row.get("emoji"))
        for row in rows:
            try:
                await self.handle_row(row, messages.get(row.get("message_id")))
            except discord.DiscordServerError:
                raise
            except FAILED as error:   # やり直しても直らないものは、知らせて次へ
                self.notify(f"変更 {row.get('seq')}（{row.get('kind')}）を反映できませんでした: {describe(error)}")
            self.store.set(CURSOR, row["seq"])

    async def _prefetch(self, rows: list[dict]) -> dict[str, dict]:
        if not self.config.sync_messages:
            return {}
        ids = set()
        for row in rows:
            kind, message_id = row.get("kind"), row.get("message_id")
            if kind == "message.create" and row.get("counted") == 1:
                place = self.store.channel_side(row.get("channel_id"))
                if place and self.config.flows_from(place[0]):
                    ids.add(message_id)
            elif kind == "message.update" and self._pair_of(message_id):   # 写しの更新はピン留めのため（ADR 0012）
                ids.add(message_id)
        return await self.nep.messages(sorted(ids)) if ids else {}

    async def handle_row(self, row: dict, message: dict | None) -> None:
        kind = row.get("kind")
        if kind == "channel.delete":
            if self.config.sync_structure:
                await self.handle_channel_delete(row["channel_id"])
        elif kind == "channel.stop":
            if self.store.channel_side(row.get("channel_id")):
                self.notify(f"チャンネル {row['channel_id']} を Neplector が読まなくなりました。同期は止まりますが、消しません")
        elif not self.config.sync_messages:
            return
        elif kind == "message.create":
            await self.handle_message_create(row, message)
        elif kind == "message.update":
            await self.handle_message_update(row, message)
            if self.config.sync_pins:
                await self.sync_pin(row["message_id"], message)
        elif kind == "message.delete":
            await self.handle_message_delete(row["message_id"])
        elif kind == "thread.update":
            await self.handle_thread_update(row["thread_id"])
        elif kind == "thread.delete":
            await self.handle_thread_delete(row["thread_id"])
        elif kind in REACTION_KINDS:
            if self.config.sync_reactions:
                await self.sync_reactions(row["message_id"])
        elif kind in POLL_VOTE_KINDS:
            pair = self._pair_of(row["message_id"])
            if pair and self.config.sync_polls:
                self.store.mark_tally_dirty(pair["source_id"])

    # ------------------------------------------------------------ discord.py の部品

    async def _channel(self, channel_id: str) -> Any:
        """チャンネルかスレッド。手元の控えに無ければ（閉じたスレッドなど）Discord に聞く。無ければ discord.NotFound。"""
        found = self.client.get_channel(int(channel_id))
        return found if found is not None else await self.client.fetch_channel(int(channel_id))

    def _guild(self, side: str) -> Any:
        guild = self.client.get_guild(int(self.config.guild_of(side)))
        if guild is None or guild.unavailable:
            raise NotReady(f"{side} のサーバーの控えがまだありません")
        return guild

    def _webhook(self, row: dict) -> Any:
        return self.partial_webhook(int(row["webhook_id"]), row["token"])

    async def _webhook_for(self, channel_id: str) -> dict:
        saved = self.store.webhook(channel_id)
        if saved:
            return saved
        channel = await self._channel(channel_id)
        me = self.client.user.id
        for hook in await channel.webhooks():
            if hook.token and hook.user is not None and hook.user.id == me:
                self.store.save_webhook(channel_id, str(hook.id), hook.token)
                return self.store.webhook(channel_id)
        hook = await channel.create_webhook(name=WEBHOOK_NAME)
        self.store.save_webhook(channel_id, str(hook.id), hook.token)
        return self.store.webhook(channel_id)

    # ------------------------------------------------------------ 発言

    async def handle_message_create(self, row: dict, message: dict | None) -> None:
        channel_id = row.get("channel_id")
        place = self.store.channel_side(channel_id)
        if place is None or row.get("counted") != 1 or message is None:   # 無いのは、外れた人か消された発言
            return
        side, target_channel = place
        message_id = row["message_id"]
        if not self.config.flows_from(side) or message.get("webhook_id"):
            return
        if self.store.message_by_source(message_id) or self.store.message_by_copy(message_id):
            return
        await self._post_copy(message, side, target_channel)

    async def _post_copy(self, message: dict, side: str, target_channel: str, retried: bool = False) -> None:
        source_thread = message["thread_id"] if message.get("thread_id") not in (None, message["channel_id"]) else None
        send, files = await self._build_send(message, side)
        target_thread: str | None = None
        creating_forum_post = False
        if source_thread:
            known = self.store.thread_side(source_thread)
            if known:
                target_thread = known[1]
            else:
                info = await self._thread_info(source_thread)
                channel = await self._channel(target_channel)
                if channel.type == structure.FORUM:
                    send["thread_name"] = (info.get("title") or "投稿")[:100]
                    tags = self._target_tags(channel, info)
                    if tags:
                        send["applied_tags"] = tags
                    creating_forum_post = True
                else:
                    created = await channel.create_thread(name=(info.get("title") or "スレッド")[:100],
                                                          type=discord.ChannelType.public_thread, auto_archive_duration=10080)
                    target_thread = str(created.id)
                    self.store.save_thread_pair(side, source_thread, target_thread, _thread_snapshot(info))
        hook = await self._webhook_for(target_channel)
        try:
            sent = await self._execute(hook, send, target_thread, files)
        except discord.NotFound as error:
            if retried:
                raise
            if error.code == UNKNOWN_WEBHOOK:      # Webhook が消されていたので作り直す
                self.store.drop_webhook(target_channel)
                self.notify(f"チャンネル {target_channel} の Webhook が消されていたので作り直しました。それまでの写しは編集・削除できません")
                return await self._post_copy(message, side, target_channel, True)
            if error.code == UNKNOWN_CHANNEL and source_thread:   # 写しのスレッドが消されていた
                self.store.drop_thread_pair(source_thread)
                return await self._post_copy(message, side, target_channel, True)
            raise
        copy_thread = target_thread
        if creating_forum_post:
            copy_thread = str(sent.channel.id)
            self.store.save_thread_pair(side, source_thread, copy_thread, _thread_snapshot(await self._thread_info(source_thread)))
        self.store.save_message_pair(message["message_id"], str(sent.id), message["author_id"], target_channel, copy_thread,
                                     source_thread, hook["webhook_id"], message.get("edited_at"))
        if self.config.sync_pins and message.get("pinned") and self.store.channel_side(target_channel):   # 写す前にピン留めされていた
            if await self._set_pin(str(sent.id), copy_thread or target_channel, other(side), True):
                self.store.set_message_pinned(message["message_id"], True)
        if send.get("poll"):
            await self._start_tally(message, source_thread, hook, str(sent.id), copy_thread)

    async def _execute(self, hook: dict, send: dict, thread_id: str | None, files: list[dict]) -> Any:
        webhook = self._webhook(hook)
        place = {"thread": discord.Object(id=int(thread_id))} if thread_id else {}
        if files:
            try:
                return await webhook.send(**send, **place, files=[discord.File(io.BytesIO(item["data"]), filename=item["filename"]) for item in files], wait=True)
            except discord.HTTPException as error:
                if not (error.status == 413 or error.code == TOO_LARGE):
                    raise
            note = "-# 📎 添付が大きすぎて同期できませんでした"
            send = {**send, "content": f"{send.get('content', '')}\n{note}".strip()[:rewrite.MAX_CONTENT]}
        return await webhook.send(**send, **place, wait=True)

    async def _build_send(self, message: dict, side: str, *, with_files: bool = True, posted_at: datetime | None = None) -> tuple[dict, list[dict]]:
        """Webhook.send に渡す値と、添付（{filename, data}）。
        posted_at：編集のときは、写しを投稿した時刻で遅れを判断する（編集した時刻で判断すると、遅れていない発言にも印が付く）。"""
        target_side = other(side)
        target_guild = self.config.guild_of(target_side)
        source_guild = self.config.guild_of(side)
        notes: list[str] = []
        files: list[dict] = []
        for index, item in enumerate(message.get("attachments") or []):
            if item.get("size") and item["size"] > self.config.max_upload_bytes:
                notes.append(f"-# 📎 添付（{item['size'] // (1024 * 1024)} MB）は {self.config.max_upload_bytes // (1024 * 1024)} MB を超えるので同期できませんでした")
            elif with_files and self.download is not None:
                try:
                    data = await self.download(item["url"])
                except (NeplectorError, Transient):
                    notes.append("-# 📎 添付を取得できませんでした")
                    continue
                files.append({"filename": rewrite.attachment_filename(index, item.get("extension"), bool(item.get("spoiler")), item.get("url"), item.get("title"), item.get("filename")),
                              "data": data})
        for sticker in message.get("stickers") or []:
            notes.append(f"-# 🏷️ スタンプ「{sticker.get('name', '')}」（同期していません）")
        poll = await self._poll(message["poll"], target_guild) if with_files and self.config.sync_polls and message.get("poll") else None
        if message.get("poll") and poll is None:
            notes.append(f"-# 📊 投票「{message['poll'].get('question', '')}」（同期していません。元のサーバーで見られます）")
        forward = message.get("forward")
        pings: set[str] = set()
        roles: set[str] | None = set() if self.config.announce_side(message.get("channel_id")) else None
        body = await self._rewrite(message.get("content") or "", source_guild, target_guild, pings=pings, roles=roles)
        forward_text = await self._rewrite(forward.get("content") or "（転送された発言）", source_guild, target_guild) if forward else None
        content = rewrite.compose(
            mark=rewrite.delay_mark(message["created_at"], posted_at or self.now(), self.config.delay_mark_minutes),
            quote=await self._reply_quote(message, side, target_guild, pings=pings),
            forward=forward_text, body=body, notes=notes)
        if not content and not files and not poll:
            content = "-# （同期できる内容がありません）"
        member = await self._member(source_guild, message["author_id"])
        send: dict[str, Any] = {"content": content, "username": rewrite.webhook_username(member.get("display_name")),
                                "allowed_mentions": _mentions(pings, roles or set())}
        if member.get("avatar_url"):
            send["avatar_url"] = member["avatar_url"]
        if poll:
            send["poll"] = poll
        flags = int(message.get("flags") or 0)
        if flags & SUPPRESS_EMBEDS:
            send["suppress_embeds"] = True
        if flags & SILENT:
            send["silent"] = True
        return send, files

    async def _rewrite(self, text: str, source_guild: str, target_guild: str, pings: set[str] | None = None, roles: set[str] | None = None) -> str:
        """pings を渡すと、相手のサーバーにいる人を本物のメンションのまま残す。元のサーバーにもいる人は通知済みなので、pings に入れない。
        引用のように pings を渡さないところは、名前の文字にする（通知しない）。
        roles を渡すと（告知だけ）、announce_ping_roles の名前のロールを相手の同じ名前のロールにして、roles に入れる。

        書き換え（rewrite.py）は通信しないので、出てくる人・ロール・チャンネルの名前を先に Neplector から読んでおく。"""
        members = {(guild, user): await self._member(guild, user)
                   for user in set(rewrite.USER_MENTION.findall(text)) for guild in (source_guild, target_guild)}
        role_names = {role: await self._role_name(role) for role in set(rewrite.ROLE_MENTION.findall(text))}
        channel_names = {channel: await self._channel_name(channel) for channel in set(rewrite.CHANNEL_MENTION.findall(text))}

        def role_map(role_id: str) -> str | None:
            if roles is None:
                return None
            name = self._guild_role_name(source_guild, role_id)
            if name is None or name not in self.config.announce_ping_roles:
                return None
            mapped = self._guild_role_id(target_guild, name)
            if mapped:
                roles.add(mapped)
            return mapped

        def keep(user: str) -> bool:
            if pings is None or not members[(target_guild, user)]:
                return False
            if not members[(source_guild, user)]:
                pings.add(user)
            return True

        return rewrite.rewrite_body(
            text,
            keep_user=keep,
            user_name=lambda user: members[(source_guild, user)].get("display_name") or members[(target_guild, user)].get("display_name"),
            role_name=role_names.get,
            channel_map=self._map_channel,
            channel_name=channel_names.get,
            message_map=self._map_message,
            target_guild=target_guild,
            role_map=role_map)

    def _place(self, channel_id: str | None) -> tuple[str, str] | None:
        """(その側, 相手のチャンネル)。同期チャンネルの組と、告知チャンネルの組（ADR 0009）。
        自動で流すかどうかは channel_side だけで決める（告知は選んだものだけ出すので）。"""
        return self.store.channel_side(channel_id) or self.config.announce_side(channel_id)

    def _map_channel(self, channel_id: str) -> str | None:
        place = self._place(channel_id) or self.store.thread_side(channel_id)
        return place[1] if place else None

    def _map_message(self, message_id: str) -> str | None:
        as_source = self.store.message_by_source(message_id)
        if as_source:
            return as_source["copy_id"]
        as_copy = self.store.message_by_copy(message_id)
        return as_copy["source_id"] if as_copy else None

    async def _reply_quote(self, message: dict, side: str, target_guild: str, *, pings: set[str] | None = None) -> str:
        """返信の引用の1行。写しへの返信なら、元の人を <@ID> にして pings に足す（ADR 0014 判断7。写しへの返信では、
        Discord はその人に知らせないため）。自分の発言の写しへの返信では呼ばない。"""
        parent_id = message.get("reply_to_id")
        if not parent_id:
            return ""
        source_guild = self.config.guild_of(side)
        link, original_id = None, parent_id
        as_source = self.store.message_by_source(parent_id)
        as_copy = self.store.message_by_copy(parent_id)
        if as_source:
            place = as_source["target_thread"] or as_source["target_channel"]
            link = f"https://discord.com/channels/{target_guild}/{place}/{as_source['copy_id']}"
        elif as_copy:   # 写しへの返信。相手のサーバーの元の発言までたどる
            original_id = as_copy["source_id"]
            place = as_copy["source_thread"]
            found = (await self.nep.messages([original_id])).get(original_id)
            if found:
                link = f"https://discord.com/channels/{target_guild}/{place or found['channel_id']}/{original_id}"
                member = await self._member(target_guild, found["author_id"])
                name = member.get("display_name") or "メンバー"
                if found["author_id"] != message.get("author_id") and str(found["author_id"]).isdigit():
                    name = f"<@{found['author_id']}>"
                    if pings is not None:
                        pings.add(str(found["author_id"]))
                return rewrite.quote_line(name, await self._rewrite(found.get("content") or "", source_guild, target_guild), link)
            return rewrite.quote_line(None, None, None)
        parent = (await self.nep.messages([original_id])).get(original_id)
        if parent is None:
            return rewrite.quote_line(None, None, None)
        member = await self._member(source_guild, parent["author_id"])
        return rewrite.quote_line(member.get("display_name") or "メンバー", await self._rewrite(parent.get("content") or "", source_guild, target_guild), link)

    async def handle_message_update(self, row: dict, message: dict | None) -> None:
        pair = self.store.message_by_source(row["message_id"])
        if pair is None or message is None or message.get("edited_at") == pair["edited_at"]:   # ピン留めなどは編集ではない
            return
        if message.get("poll"):   # 投票は作った後に編集できない
            self.store.set_message_edited(row["message_id"], message.get("edited_at"))
            return
        hook = self.store.webhook_by_id(pair["webhook_id"])
        if hook is None:
            self.notify(f"写し {pair['copy_id']} は Webhook が変わったので編集できません")
            return
        place = self._place(message["channel_id"])
        side = place[0] if place else "a"
        send, _ = await self._build_send(message, side, with_files=False, posted_at=rewrite.snowflake_time(pair["copy_id"]))
        try:
            await self._edit(hook, pair["copy_id"], pair["target_thread"], send["content"])
        except discord.NotFound as error:
            if error.code not in (UNKNOWN_MESSAGE, UNKNOWN_WEBHOOK):
                raise
            self.store.drop_message(row["message_id"])
            return
        self.store.set_message_edited(row["message_id"], message.get("edited_at"))

    async def _edit(self, hook: dict, message_id: str, thread_id: str | None, content: str) -> None:
        place = {"thread": discord.Object(id=int(thread_id))} if thread_id else {}
        await self._webhook(hook).edit_message(int(message_id), content=content, allowed_mentions=NO_PINGS, **place)

    async def _delete_message(self, hook: dict, message_id: str, thread_id: str | None) -> None:
        place = {"thread": discord.Object(id=int(thread_id))} if thread_id else {}
        await self._webhook(hook).delete_message(int(message_id), **place)

    async def handle_message_delete(self, message_id: str) -> None:
        pair = self.store.message_by_source(message_id)
        if pair:
            await self._delete_copy(pair)
            return
        as_copy = self.store.message_by_copy(message_id)
        if as_copy:   # 写しが消されても、元の発言は消さない
            await self._delete_tally(as_copy["source_id"])
            self.store.drop_message(as_copy["source_id"])

    async def _delete_copy(self, pair: dict) -> None:
        await self._delete_tally(pair["source_id"])
        hook = self.store.webhook_by_id(pair["webhook_id"])
        if hook is None:
            self.notify(f"写し {pair['copy_id']}（チャンネル {pair['target_channel']}）を消せません。Webhook が変わったので、手で消してください")
            self.store.drop_message(pair["source_id"])
            return
        try:
            await self._delete_message(hook, pair["copy_id"], pair["target_thread"])
        except discord.NotFound as error:
            if error.code == UNKNOWN_WEBHOOK:
                self.notify(f"写し {pair['copy_id']}（チャンネル {pair['target_channel']}）を消せません。Webhook が消されたので、手で消してください")
            elif error.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL):
                raise
        self.store.drop_message(pair["source_id"])

    # ------------------------------------------------------------ 告知（ADR 0009）

    async def announce_now(self) -> None:
        """コマンドから呼ぶ。同期の本体の回を待たずに、告知の依頼を処理する。"""
        async with self.lock:
            await self.process_announcements()

    async def process_announcements(self) -> None:
        """右クリックのメニューで頼まれた「出す」「下げる」を処理する。結果は記号で残し、コマンドが読んで答える。"""
        clock = self.now().timestamp()
        self.store.purge_announces(clock - ANNOUNCE_KEEP_SECONDS)
        for request in self.store.pending_announces():
            try:
                result, link, *rest = await self._announce(request, clock)
            except TRANSIENT:
                continue   # 次の回にやり直す
            except FAILED as error:
                self.notify(f"告知 {request['message_id']} を{'出せ' if request['action'] == SHARE else '下げられ'}ませんでした: {describe(error)}")
                result, link, rest = "failed", None, []
            if result != "wait":
                self.store.finish_announce(request["message_id"], result, link, rest[0] if rest else None)

    async def _announce(self, request: dict, clock: float) -> tuple:
        message_id = request["message_id"]
        place = self.config.announce_side(request["channel_id"])
        if place is None:
            return "not_announcement", None
        side, target_channel = place
        if request["action"] == WITHDRAW:
            pair = self.store.message_by_source(message_id) or self.store.message_by_copy(message_id)
            if pair is None:
                return "not_shared", None
            await self._delete_copy(pair)
            return "withdrawn", None
        if self.store.message_by_copy(message_id):
            return "is_copy", None
        pair = self.store.message_by_source(message_id)
        if pair:
            return "shared", self._copy_link(other(side), pair)
        if not self.config.flows_from(side):
            return "direction", None
        message = (await self.nep.messages([message_id])).get(message_id)
        if message is None:   # まだ記録が届いていないか、外れた人の発言か、読んでいないチャンネル
            return ("wait", None) if clock - request["requested_at"] < ANNOUNCE_WAIT_SECONDS else ("no_record", None)
        if message.get("channel_id") != request["channel_id"] or message.get("thread_id") not in (None, message["channel_id"]):
            return "not_announcement", None
        if message.get("content") is None or message.get("webhook_id"):   # 本文を開く日より前の発言か、bot の投稿
            return "no_body", None
        # 相手のサーバーで開けないリンク。出すのは止めず、押した人に知らせる（判断5）
        warning = rewrite.dead_links(message.get("content") or "", self.config.guild_of(side), self._map_channel, self._map_message)
        await self._post_copy(message, side, target_channel)
        pair = self.store.message_by_source(message_id)
        return ("shared", self._copy_link(other(side), pair), warning) if pair else ("failed", None)

    def _copy_link(self, target_side: str, pair: dict) -> str:
        place = pair["target_thread"] or pair["target_channel"]
        return f"https://discord.com/channels/{self.config.guild_of(target_side)}/{place}/{pair['copy_id']}"

    # ------------------------------------------------------------ ピン留め（ADR 0012）

    async def sync_pin(self, message_id: str, message: dict | None) -> None:
        """知らせが来た発言の今のピン留めを控えと比べ、違えば相手を同じにする。
        相手の値は読まない（Neplector が相手の記録をまだ新しくしていないと、外されたと見誤る）。
        bot が付けた知らせも戻ってくるが、控えと同じなので何もしない（輪にならない）。"""
        pair = self._pair_of(message_id)
        if pair is None or message is None:   # 無いのは、外れた人か消された発言
            return
        place = self.store.channel_side(pair["target_channel"])   # 告知チャンネルの組は対象にしない
        if place is None:
            return
        target_side, source_channel = place
        pinned = bool(message.get("pinned"))
        if pinned == bool(pair["pinned"]):
            return
        if message_id == pair["source_id"]:
            changed_side, write = other(target_side), (pair["copy_id"], pair["target_thread"] or pair["target_channel"], target_side)
        else:
            changed_side, write = target_side, (pair["source_id"], pair["source_thread"] or source_channel, other(target_side))
        if not self.config.flows_from(changed_side):
            return
        if await self._set_pin(*write, pinned):
            self.store.set_message_pinned(pair["source_id"], pinned)

    async def _set_pin(self, message_id: str, place: str, side: str, pinned: bool) -> bool:
        """ピン留めを付ける・外す。控えを新しくしてよければ True。"""
        target = self.client.get_partial_messageable(int(place), guild_id=int(self.config.guild_of(side))).get_partial_message(int(message_id))
        try:
            if pinned:
                await target.pin(reason="NepSync: 相手のサーバーのピン留めに合わせる")
            else:
                await target.unpin(reason="NepSync: 相手のサーバーのピン留めに合わせる")
        except discord.NotFound:   # 発言が消されている。もうすることが無い
            return True
        except discord.Forbidden:
            self.notify(f"{side} のチャンネル {place} で、bot に「メッセージのピン留め」の権限が無いので、ピン留めを写せませんでした")
            return False
        except discord.HTTPException as error:
            if error.code != TOO_MANY_PINS:
                raise
            self.notify(f"{side} のチャンネル {place} のピン留めが上限（250件）なので、ピン留めを写せませんでした")
            return False
        return True

    # ------------------------------------------------------------ リアクション

    def _pair_of(self, message_id: str | None) -> dict | None:
        if not message_id:
            return None
        return self.store.message_by_source(message_id) or self.store.message_by_copy(message_id)

    async def sync_reactions(self, message_id: str) -> None:
        """両方の発言の今の人のリアクションを見て、bot の分をそろえる。知らせの順番に頼らないので、取りこぼしても次で直る。
        bot のリアクションは Neplector に記録されないので、輪にならない。"""
        pair = self._pair_of(message_id)
        if pair is None or pair["source_id"] in self._reactions_done:
            return
        self._reactions_done.add(pair["source_id"])
        place = self._place(pair["target_channel"])
        if place is None:
            return
        target_side, source_channel = place
        source_side = other(target_side)
        found = await self.nep.messages([pair["source_id"], pair["copy_id"]])
        original, copy = found.get(pair["source_id"]), found.get(pair["copy_id"])
        if original is None:   # 外れた人の発言か、消された発言
            return
        await self._apply_reactions(pair["copy_id"], pair["target_thread"] or pair["target_channel"], target_side, original.get("reactions") or [])
        if copy is not None and self.config.flows_from(target_side):
            await self._apply_reactions(pair["source_id"], pair["source_thread"] or source_channel, source_side, copy.get("reactions") or [])

    async def clear_reactor_names(self) -> None:
        """やめた機能（写しの末尾に、リアクションした人の名前を書く）の後片付け。書いた写しを、名前の無い本文に戻す（ADR 0004 判断7）。"""
        for source_id in self.store.reactor_line_sources():
            pair = self.store.message_by_source(source_id)
            original = (await self.nep.messages([source_id])).get(source_id) if pair else None
            hook = self.store.webhook_by_id(pair["webhook_id"]) if pair else None
            place = self._place(pair["target_channel"]) if pair else None
            if original is not None and hook is not None and place is not None and not original.get("poll"):
                send, _ = await self._build_send(original, other(place[0]), with_files=False, posted_at=rewrite.snowflake_time(pair["copy_id"]))
                try:
                    await self._edit(hook, pair["copy_id"], pair["target_thread"], send["content"])
                except discord.NotFound as error:
                    if error.code not in (UNKNOWN_MESSAGE, UNKNOWN_WEBHOOK, UNKNOWN_CHANNEL):
                        raise
            self.store.drop_reactor_line(source_id)
        self._names_cleared = True

    async def _apply_reactions(self, message_id: str, place: str, side: str, reactions: list[dict]) -> None:
        guild_id = self.config.guild_of(side)
        wanted = set()
        for emoji in sorted({item["emoji"] for item in reactions}):
            mapped = await self._emoji_for(emoji, guild_id)
            if mapped:
                wanted.add(mapped)
            else:
                self.notify(f"カスタム絵文字「{emoji.split(':')[0]}」が相手のサーバーに無いので、リアクションを写しません。同じ名前で登録すると写ります")
        have = set(self.store.bot_reactions(message_id))
        target = self.client.get_partial_messageable(int(place), guild_id=int(guild_id)).get_partial_message(int(message_id))
        for emoji in sorted(wanted - have):
            try:
                await target.add_reaction(emoji)
            except discord.NotFound as error:
                if error.code not in (UNKNOWN_MESSAGE, UNKNOWN_EMOJI):
                    raise
                continue
            self.store.add_bot_reaction(message_id, emoji)
        for emoji in sorted(have - wanted):
            try:
                await target.remove_reaction(emoji, self.client.user)
            except discord.NotFound:
                pass
            self.store.drop_bot_reaction(message_id, emoji)

    async def _emoji_for(self, emoji: str, guild_id: str) -> str | None:
        """相手のサーバーで使える形（ふつうの絵文字か「名前:ID」）。カスタム絵文字は同じ名前のものを探す（ID はサーバーごとに違う）。"""
        if ":" not in emoji:
            return emoji
        found = await self._emoji_row(emoji.split(":")[0], guild_id)
        return f"{found['name']}:{found['emoji_id']}" if found else None

    async def _emoji_row(self, name: str, guild_id: str) -> dict:
        key = ("emoji", guild_id, name)

        async def fetch() -> dict:
            rows = [row for row in await self.nep.table("emojis", guild_id=guild_id, name=name) if row.get("available")]
            return rows[0] if rows else {}
        found = await self._cached(key, fetch)
        if not found:
            self._cache.pop(key, None)   # 登録されたら次から使う
        return found

    # ------------------------------------------------------------ 投票

    async def _poll(self, poll: dict, guild_id: str) -> discord.Poll | None:
        """相手のサーバーに作る投票。締め切りを過ぎていれば作らない。期間は時間単位なので、切り上げる。"""
        hours = 24
        if poll.get("expires_at"):
            remaining = (_parse_time(poll["expires_at"]) - self.now()).total_seconds()
            if remaining <= 0:
                return None
            hours = min(math.ceil(remaining / 3600), MAX_POLL_HOURS)
        made = discord.Poll(question=(poll.get("question") or "")[:300], duration=timedelta(hours=hours), multiple=bool(poll.get("multiselect")))
        for answer in sorted(poll.get("answers") or [], key=lambda item: item["answer_id"]):
            emoji: Any = None
            if answer.get("emoji"):
                mapped = await self._emoji_for(answer["emoji"], guild_id)
                if mapped and ":" in mapped:
                    name, emoji_id = mapped.split(":")
                    emoji = discord.PartialEmoji(name=name, id=int(emoji_id))
                elif mapped:
                    emoji = mapped
            made.add_answer(text=(answer.get("text") or "")[:55], emoji=emoji)
        return made

    async def _start_tally(self, message: dict, source_thread: str | None, copy_hook: dict, copy_id: str, copy_thread: str | None) -> None:
        """両方のサーバーに合計の発言を1つずつ出す。失敗しても投票の写しは残す。"""
        try:
            poll = message["poll"]
            send = {"content": _tally_text(poll, {}, closed=False), "username": WEBHOOK_NAME, "allowed_mentions": NO_PINGS}
            source_hook = await self._webhook_for(message["channel_id"])
            source_sent = await self._execute(source_hook, send, source_thread, [])
            copy_sent = await self._execute(copy_hook, send, copy_thread, [])
        except discord.DiscordServerError:
            raise
        except discord.HTTPException as error:
            self.notify(f"投票 {message['message_id']} の合計の発言を出せませんでした: {describe(error)}")
            return
        self.store.save_tally(message["message_id"], poll.get("expires_at"), str(source_sent.id), source_hook["webhook_id"], source_thread,
                              str(copy_sent.id), copy_hook["webhook_id"], copy_thread, self.now().timestamp())

    async def flush_tallies(self) -> None:
        """票が変わった投票の合計を書き換える。締め切りを過ぎたら最後に1回書いて終わる。"""
        clock = self.now()
        stamp = clock.timestamp()
        for row in self.store.open_tallies():
            expired = _is_past(row["expires_at"], clock)
            if not expired and not (row["dirty"] and stamp - row["sent_at"] >= TALLY_SECONDS):
                continue
            pair = self.store.message_by_source(row["source_id"])
            if pair is None:
                self.store.drop_tally(row["source_id"])
                continue
            found = await self.nep.messages([pair["source_id"], pair["copy_id"]])
            original = found.get(pair["source_id"])
            if original is None or not original.get("poll"):   # 外れた人の投票か、消された投票
                await self._delete_tally(row["source_id"])
                continue
            copy = found.get(pair["copy_id"])
            votes = {(vote["answer_id"], vote["user_id"]) for item in (original, copy) if item and item.get("poll") for vote in item["poll"].get("votes") or []}
            counts: dict[int, int] = {}
            for answer_id, _ in votes:
                counts[answer_id] = counts.get(answer_id, 0) + 1
            encoded = json.dumps(counts, sort_keys=True)
            if encoded == row["counts"] and not expired:
                self.store.clean_tally(row["source_id"])
                continue
            text = _tally_text(original["poll"], counts, closed=expired)
            ok = await self._edit_tally(row["source_webhook"], row["source_tally"], row["source_thread"], text)
            ok = await self._edit_tally(row["copy_webhook"], row["copy_tally"], row["copy_thread"], text) and ok
            if ok:
                self.store.update_tally(row["source_id"], encoded, stamp, expired)
            else:
                self.notify(f"投票 {row['source_id']} の合計の発言を更新できないので、更新をやめます")
                self.store.close_tally(row["source_id"])

    async def _edit_tally(self, webhook_id: str, message_id: str, thread_id: str | None, text: str) -> bool:
        hook = self.store.webhook_by_id(webhook_id)
        if hook is None:
            return False
        try:
            await self._edit(hook, message_id, thread_id, text)
        except discord.NotFound:
            return False
        return True

    async def _delete_tally(self, source_id: str) -> None:
        row = self.store.tally(source_id)
        if row is None:
            return
        for webhook_id, message_id, thread_id in ((row["source_webhook"], row["source_tally"], row["source_thread"]), (row["copy_webhook"], row["copy_tally"], row["copy_thread"])):
            hook = self.store.webhook_by_id(webhook_id)
            if hook is None:
                continue
            try:
                await self._delete_message(hook, message_id, thread_id)
            except discord.NotFound:
                pass
        self.store.drop_tally(source_id)

    # ------------------------------------------------------------ スレッド

    async def handle_thread_update(self, thread_id: str) -> None:
        known = self.store.thread_side(thread_id)
        if known is None:
            return
        side, copy_id, snapshot = known
        if not self.config.flows_from(side):
            return
        info = await self._thread_info(thread_id)
        if not info:
            return
        new = _thread_snapshot(info)
        if new == snapshot:
            return
        snapshot = snapshot or {}
        thread = await self._channel(copy_id)
        options: dict[str, Any] = {}
        if new["title"] != snapshot.get("title"):
            options["name"] = (new["title"] or "スレッド")[:100]
        if new["tags"] != snapshot.get("tags"):
            forum = await self._channel(str(thread.parent_id))
            if forum.type == structure.FORUM:
                options["applied_tags"] = self._target_tags(forum, info)
        if new["locked"] != snapshot.get("locked"):
            options["locked"] = bool(new["locked"])
        if new["archived"] != snapshot.get("archived"):
            options["archived"] = bool(new["archived"])
        if options:
            await thread.edit(**options)
        self.store.save_thread_pair(side, thread_id, copy_id, new)

    async def handle_thread_delete(self, thread_id: str) -> None:
        known = self.store.thread_side(thread_id)
        if known is None:
            return
        side, copy_id, _ = known
        if self.config.flows_from(side) and await self._really_gone(thread_id):
            await self._delete_channel(copy_id)
        self.store.drop_thread_pair(thread_id)

    async def _thread_info(self, thread_id: str) -> dict:
        rows = await self.nep.table("threads", thread_id=thread_id)
        return rows[0] if rows else {}

    def _target_tags(self, forum: Any, info: dict) -> list:
        """タグは名前で対応させる。Neplector の tags は名前の一覧。返すのは相手のフォーラムの ForumTag。"""
        names = json.loads(info.get("tags") or "[]")
        available = {tag.name: tag for tag in forum.available_tags}
        return [available[name] for name in names if name in available][:5]

    # ------------------------------------------------------------ 名前（Neplector）

    async def _cached(self, key: tuple, fetch: Callable[[], Awaitable[Any]]) -> Any:
        clock = time.monotonic()
        hit = self._cache.get(key)
        if hit and clock - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = await fetch()
        self._cache[key] = (clock, value)
        return value

    async def _member(self, guild_id: str, user_id: str) -> dict:
        """投稿者の名前とアイコン。名簿にまだ無い人は覚えず、毎回聞き直す（名簿が入った次の発言から正しく出る）。"""
        key = ("member", guild_id, user_id)

        async def fetch() -> dict:
            rows = await self.nep.table("member_servers", guild_id=guild_id, user_id=user_id)
            return rows[0] if rows else {}
        member = await self._cached(key, fetch)
        if not member:
            self._cache.pop(key, None)
        return member

    async def _role_name(self, role_id: str) -> str | None:
        async def fetch() -> str | None:
            rows = await self.nep.table("roles", role_id=role_id)
            return rows[0]["name"] if rows else None
        return await self._cached(("role", role_id), fetch)

    async def _channel_name(self, channel_id: str) -> str | None:
        async def fetch() -> str | None:
            rows = await self.nep.table("channels", channel_id=channel_id)
            return rows[0]["name"] if rows else None
        return await self._cached(("channel", channel_id), fetch)

    def _guild_role_name(self, guild_id: str, role_id: str) -> str | None:
        guild = self.client.get_guild(int(guild_id))
        role = guild.get_role(int(role_id)) if guild is not None else None
        return role.name if role is not None else None

    def _guild_role_id(self, guild_id: str, name: str) -> str | None:
        """同じ名前のロールが1つだけあるときに、その ID。複数あればどれか決められないので None。"""
        guild = self.client.get_guild(int(guild_id))
        found = [str(role.id) for role in (guild.roles if guild is not None else []) if role.name == name]
        return found[0] if len(found) == 1 else None

    # ------------------------------------------------------------ 構造

    async def sync_structure(self, *, dry_run: bool = False) -> list[str]:
        """両方のカテゴリを見比べて、差を直す。dry_run なら、することの一覧を返すだけで何も変えない。"""
        actions: list[str] = []
        guild_a, guild_b = self._guild("a"), self._guild("b")
        channels_a, channels_b = list(guild_a.channels), list(guild_b.channels)
        for category in self._ensure_category_pairs(channels_a, channels_b, actions, dry_run):
            await self._sync_category(category, channels_a, channels_b, actions, dry_run)
        return actions

    def _ensure_category_pairs(self, channels_a: list, channels_b: list, actions: list[str], dry_run: bool) -> list[dict]:
        """組の一覧を返す。dry_run では保存しないが、この先の見比べには入れる。"""
        pairs = self.store.category_pairs()
        known = {pair["a_id"] for pair in pairs}
        for name in self.config.category_names:
            left = [item for item in channels_a if item.type == discord.ChannelType.category and item.name == name]
            right = [item for item in channels_b if item.type == discord.ChannelType.category and item.name == name]
            if left and str(left[0].id) in known:
                continue
            if len(left) != 1 or len(right) != 1:
                self._notice(f"カテゴリ「{name}」を両方のサーバーで1つずつ見つけられません（A: {len(left)} / B: {len(right)}）。bot の権限と名前を確かめてください", actions, dry_run)
                continue
            actions.append(f"カテゴリを組にする: {name}")
            pairs.append({"a_id": str(left[0].id), "b_id": str(right[0].id), "snapshot": json.dumps({"name": name})})
            if not dry_run:
                self.store.save_category_pair(str(left[0].id), str(right[0].id), {"name": name})
        return pairs

    async def _sync_category(self, category: dict, channels_a: list, channels_b: list, actions: list[str], dry_run: bool) -> None:
        flow_a, flow_b = self.config.flows_from("a"), self.config.flows_from("b")
        a_id, b_id = category["a_id"], category["b_id"]
        await self._sync_category_name(category, channels_a, channels_b, actions, dry_run, flow_a, flow_b)
        children_a = [item for item in channels_a if str(item.category_id) == a_id]
        children_b = [item for item in channels_b if str(item.category_id) == b_id]
        readable = {row["channel_id"] for side_id in (a_id, b_id) for row in await self.nep.table("channels", category_id=side_id) if row.get("reading") == 1}
        hidden = readable - {str(item.id) for item in children_a + children_b}
        for channel_id in sorted(hidden):
            self._notice(f"チャンネル {channel_id} は Neplector が読んでいますが、bot に見えません。チャンネルの権限を確かめてください", actions, dry_run)
        pairs = self.store.channel_pairs(a_id)
        result = structure.plan(children_a, children_b, pairs, readable, flow_a=flow_a, flow_b=flow_b, priority=self.config.priority)
        for text in result.notices:
            self._notice(text, actions, dry_run)
        for pair_a, pair_b in result.new_pairs:
            actions.append(f"同じ名前のチャンネルを組にする: {pair_a} = {pair_b}")
            if not dry_run:
                self.store.save_channel_pair(pair_a, pair_b, a_id, None)
        for side, source in result.creates:
            actions.append(f"{side} 側に作る: {source.name}（{'フォーラム' if source.type == structure.FORUM else 'テキスト'}）")
            if not dry_run:
                await self._create_copy(side, source, a_id if side == "a" else b_id, a_id)
        for missing_side, channel_id in result.missing:
            await self._handle_missing(missing_side, channel_id, actions, dry_run)
        for pair_a, pair_b, change in result.updates:
            await self._apply_update(pair_a, pair_b, a_id, change, actions, dry_run)
        if result.new_pairs or result.creates:
            return    # 新しい組は次の見回りで中身と並び順をそろえる
        await self._sync_order(category, children_a, children_b, pairs, actions, dry_run, flow_a, flow_b)

    async def _sync_category_name(self, category: dict, channels_a: list, channels_b: list, actions: list[str], dry_run: bool, flow_a: bool, flow_b: bool) -> None:
        left = next((item for item in channels_a if str(item.id) == category["a_id"]), None)
        right = next((item for item in channels_b if str(item.id) == category["b_id"]), None)
        if left is None or right is None:
            return
        snapshot = json.loads(category["snapshot"]) if category.get("snapshot") else None
        change = structure.merge({"name": left.name}, {"name": right.name}, snapshot, flow_a=flow_a, flow_b=flow_b, priority=self.config.priority)
        if change.write_a:
            actions.append(f"a のカテゴリ名を変える: {change.write_a['name']}")
        if change.write_b:
            actions.append(f"b のカテゴリ名を変える: {change.write_b['name']}")
        if dry_run:
            return
        if change.write_a:
            await left.edit(name=change.write_a["name"])
        if change.write_b:
            await right.edit(name=change.write_b["name"])
        if change.snapshot != snapshot:
            self.store.save_category_pair(category["a_id"], category["b_id"], change.snapshot)

    async def _create_copy(self, side: str, source: Any, parent_id: str, category_a: str) -> None:
        guild = self._guild(side)
        fields = structure.fields_of(source)
        options: dict[str, Any] = {"category": guild.get_channel(int(parent_id))}
        if fields["topic"]:
            options["topic"] = fields["topic"]
        if fields["rate_limit_per_user"]:
            options["slowmode_delay"] = fields["rate_limit_per_user"]
        if fields["nsfw"]:
            options["nsfw"] = True
        if source.type == structure.FORUM:
            if fields["tags"]:
                options["available_tags"] = [_new_tag(tag) for tag in fields["tags"]]
            created = await guild.create_forum(fields["name"], **options)
        else:
            created = await guild.create_text_channel(fields["name"], **options)
        pair = (str(source.id), str(created.id)) if side == "b" else (str(created.id), str(source.id))
        self.store.save_channel_pair(pair[0], pair[1], category_a, fields)

    async def _handle_missing(self, side: str, channel_id: str, actions: list[str], dry_run: bool) -> None:
        """一覧に無いだけでは消さない。写しが手で消されたときだけ、組を外して作り直せるようにする。
        「無い」は手元の控えでなく、Discord に直接聞いて確かめる。"""
        try:
            await self.client.fetch_channel(int(channel_id))
            return    # 別のカテゴリへ移されたなど。組はそのまま
        except discord.Forbidden:
            self._notice(f"チャンネル {channel_id} が bot に見えなくなりました。権限を確かめてください", actions, dry_run)
            return
        except discord.NotFound as error:
            if error.code != UNKNOWN_CHANNEL:
                raise
        if self.config.flows_from(side):
            return    # 元が消されたときは channel.delete で扱う
        actions.append(f"写し {channel_id} が消されているので、組を外して作り直す")
        if not dry_run:
            self.store.drop_channel_pair(channel_id)
            self.notify(f"写しのチャンネル {channel_id} が手で消されていたので、作り直します。それまでの写しは戻りません")

    async def _apply_update(self, pair_a: str, pair_b: str, category_a: str, change: structure.Merge, actions: list[str], dry_run: bool) -> None:
        for side, channel_id, write in (("a", pair_a, change.write_a), ("b", pair_b, change.write_b)):
            if not write:
                continue
            actions.append(f"{side} のチャンネル {channel_id} を変える: {sorted(write)}")
            if not dry_run:
                channel = self._guild(side).get_channel(int(channel_id))
                await channel.edit(**_edit_options(channel, write))
        for key in change.conflicts:
            self._notice(f"チャンネル {pair_a} / {pair_b} の {key} が両方で変わったので、{self.config.priority} 側に合わせました", actions, dry_run)
        if not dry_run:
            self.store.save_channel_pair(pair_a, pair_b, category_a, change.snapshot)

    async def _sync_order(self, category: dict, children_a: list, children_b: list, pairs: list[dict], actions: list[str], dry_run: bool, flow_a: bool, flow_b: bool) -> None:
        """並び順をそろえる。discord.py の move は1つずつ動かすので、目標を先に控えに残してから動かす。
        途中で止まっても、次の回は同じ目標の続きをやる（動かしかけの並びを、人が変えたものと見間違えないため）。"""
        key, goal_key = f"order:{category['a_id']}", f"order_goal:{category['a_id']}"
        goal = json.loads(self.store.get(goal_key) or "null")
        if goal is None:
            saved = self.store.get(key)
            side, order, new_order = structure.reorder_plan(children_a, children_b, pairs, json.loads(saved) if saved else None,
                                                            flow_a=flow_a, flow_b=flow_b, priority=self.config.priority)
            if not dry_run:
                self.store.set(key, json.dumps(new_order))
            if side is None:
                return
            goal = {"side": side, "order": order}
            actions.append(f"{side} の並び順を変える")
            if dry_run:
                return
            self.store.set(goal_key, json.dumps(goal))
        elif dry_run:
            actions.append(f"{goal['side']} の並び順を変える（続き）")
            return
        side = goal["side"]
        await self._reorder(side, category[f"{side}_id"], goal["order"])
        self.store.unset(goal_key)

    async def _reorder(self, side: str, category_id: str, goal: list[str]) -> None:
        guild = self._guild(side)
        for _ in range(2 * len(goal) + 1):
            children = [item for item in guild.channels if str(item.category_id) == category_id and item.type in TEXT_BUCKET]
            present = {str(item.id) for item in children}
            wanted = [item for item in goal if item in present]
            current = [item for item in structure.order_of(children) if item in set(wanted)]
            move = structure.next_move(current, wanted)
            if move is None:
                return
            moving, before = guild.get_channel(int(move[0])), guild.get_channel(int(move[1]))
            expected = [item for item in structure.order_of(children) if item != move[0]]
            expected.insert(expected.index(move[1]), move[0])
            await moving.move(before=before)
            await self._settled(guild, category_id, expected)
        raise NotReady("並び順がそろいません")

    async def _settled(self, guild: Any, category_id: str, expected: list[str]) -> None:
        """move は Discord に送るだけで、手元の控えは知らせ（GUILDS）で変わる。次の1手は、控えが追いついてから計算する。"""
        deadline = time.monotonic() + SETTLE_SECONDS
        while True:
            children = [item for item in guild.channels if str(item.category_id) == category_id and item.type in TEXT_BUCKET]
            if structure.order_of(children) == expected:
                return
            if time.monotonic() > deadline:
                raise NotReady("並べ替えの知らせが届きません")
            await asyncio.sleep(0.2)

    def _notice(self, text: str, actions: list[str], dry_run: bool) -> None:
        actions.append(f"知らせ: {text}")
        if not dry_run:
            self.notify(text)

    # ------------------------------------------------------------ チャンネルの削除（元に戻せない）

    async def handle_channel_delete(self, channel_id: str) -> None:
        place = self.store.channel_side(channel_id)
        if place is None:
            return    # 自分が消した写しの知らせも、ここで止まる
        side, target_id = place
        if not self.config.flows_from(side):
            return
        if not self.config.allow_delete:
            self.notify(f"チャンネル {channel_id} が消されました。自動の削除を止めているので、写し {target_id} は残しています")
            return
        if not await self._really_gone(channel_id):
            self.notify(f"チャンネル {channel_id} の削除の知らせが来ましたが、Discord では消えていないか確かめられないので、写し {target_id} は消しません")
            return
        clock = self.now().timestamp()
        if self.store.deletes_since(clock - 3600) >= self.config.delete_limit_per_hour:
            self.store.hold_delete(channel_id, clock)
            self.notify(f"1時間に消す上限（{self.config.delete_limit_per_hour}）を超えたので、写し {target_id} の削除を止めました。確かめて `python -m nepsync approve-deletes` で進めてください")
            return
        await self._delete_target(channel_id, target_id, clock)

    async def _delete_target(self, channel_id: str, target_id: str, clock: float) -> None:
        await self._delete_channel(target_id)
        self.store.log_delete(clock, channel_id, target_id)
        self.store.drop_channel_pair(channel_id)
        self.store.release_held(channel_id)
        self.notify(f"チャンネル {channel_id} が消されたので、写し {target_id} を消しました")

    async def _delete_channel(self, channel_id: str) -> None:
        """写しのチャンネルかスレッドを消す。もう無ければ（404）それでよい。"""
        try:
            channel = await self._channel(channel_id)
            await channel.delete()
        except discord.NotFound:
            pass

    async def approve_held_deletes(self) -> int:
        """止めていた削除を、運営者の確認のあとで進める。上限は数えない。"""
        done = 0
        for channel_id in self.store.held_deletes():
            place = self.store.channel_side(channel_id)
            if place is None or not await self._really_gone(channel_id):
                self.store.release_held(channel_id)
                continue
            await self._delete_target(channel_id, place[1], self.now().timestamp())
            done += 1
        return done

    async def _really_gone(self, channel_id: str) -> bool:
        """Discord が「無い」（404・Unknown Channel）と答えたときだけ True。403 は「見えない」なので消えたとしない。
        手元の控え（get_channel）は使わない。控えに無いことは、消えたことの確かめにならない。"""
        try:
            await self.client.fetch_channel(int(channel_id))
            return False
        except discord.NotFound as error:
            return error.code == UNKNOWN_CHANNEL
        except discord.Forbidden:
            return False

    # ------------------------------------------------------------ 忘れるもの

    async def process_forget(self) -> None:
        if self.avatars is not None:   # サーバーにいなくなった人のアイコンも消す
            members = {(row["guild_id"], row["user_id"]) for guild_id in (self.config.guild_a, self.config.guild_b)
                       for row in await self.nep.table("member_servers", guild_id=guild_id)}
            await self.avatars.prune(members)
        after = int(self.store.get(FORGET_CURSOR) or 0)
        while True:
            body = await self.nep.forget(after)
            rows = body.get("forget", [])
            for row in rows:
                await self._forget_one(row)
                after = row["seq"]
                self.store.set(FORGET_CURSOR, after)
            if not rows:
                return

    async def _forget_one(self, row: dict) -> None:
        kind, item = row.get("kind"), row.get("id")
        if kind == "user":
            for pair in self.store.messages_by_author(item):
                await self._delete_copy(pair)
            self.store.set_hidden(item, False)
            self.store.forget_roster_user(item)
            if self.avatars is not None:
                await self.avatars.remove_user(item)
        elif kind == "message":
            pair = self.store.message_by_source(item)
            if pair:
                await self._delete_copy(pair)
        elif kind == "thread":
            known = self.store.thread_side(item)
            if known:
                await self._delete_channel(known[1])
                self.store.drop_thread_pair(item)

    # ------------------------------------------------------------ ずっと回す

    async def run_forever(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        pause = 5.0
        stalled_since: float | None = None   # 一時的な失敗が続き始めた時刻
        told = False                         # 続いていることを、もう知らせたか
        while True:
            try:
                await self.run_once()
                pause = 5.0
                if told:
                    self.notify("つながりが戻りました。止まっていた間の発言を、続きから流します")
                stalled_since, told = None, False
                continue
            except TRANSIENT as error:
                self.log(f"一時的な失敗。{pause:.0f} 秒後にやり直します: {why(error)}")
                stamp = self.now().timestamp()
                stalled_since = stamp if stalled_since is None else stalled_since
                if not told and stamp - stalled_since >= STALL_NOTICE_SECONDS:
                    told = True   # 知らせは1回だけ。ログには毎回書く
                    self.notify(f"Neplector か Discord に{STALL_NOTICE_SECONDS // 60}分つながらず、同期が止まっています（{why(error)}）。"
                                "つながれば続きから流します（30日以内）")
            except FAILED as error:   # キーが消された、など。読む位置は進めない
                self.notify(f"同期が進んでいません: {describe(error)}。やり直します")
            except Exception as error:  # 想定していない失敗。同じ行からやり直す。値を出さないよう、種類と場所だけ書く
                place = "".join(traceback.format_tb(error.__traceback__)[-3:])
                self.log(f"想定していない失敗 {type(error).__name__}\n{place}")
                self.notify(f"想定していない失敗で同期が進んでいません（{type(error).__name__}）。ログを見てください")
            await sleep(pause)
            pause = min(pause * 2, 300.0)


def _mentions(users: set[str], roles: set[str]) -> discord.AllowedMentions:
    """だれも呼ばないのが基本。相手のサーバーにだけいる人と、告知で鳴らすロールだけ呼ぶ。"""
    return discord.AllowedMentions(everyone=False, replied_user=False,
                                   users=[discord.Object(id=int(item)) for item in sorted(users)[:100]] or False,
                                   roles=[discord.Object(id=int(item)) for item in sorted(roles)[:100]] or False)


def _edit_options(channel: Any, write: dict) -> dict:
    """控えの項目名を、discord.py の channel.edit の名前にする。"""
    names = {"name": "name", "topic": "topic", "rate_limit_per_user": "slowmode_delay", "nsfw": "nsfw"}
    options = {names[key]: value for key, value in write.items() if key in names}
    if "tags" in write:   # ID を保つために、同じ名前のタグは今の ID のまま送る
        existing = {tag.name: tag for tag in channel.available_tags}
        tags = []
        for tag in write["tags"]:
            made = _new_tag(tag)
            if tag["name"] in existing:
                made.id = existing[tag["name"]].id
            tags.append(made)
        options["available_tags"] = tags
    return options


def _new_tag(tag: dict) -> discord.ForumTag:
    return discord.ForumTag(name=tag["name"], moderated=bool(tag.get("moderated")), emoji=tag.get("emoji_name") or None)


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_past(expires_at: str | None, now: datetime) -> bool:
    return bool(expires_at) and _parse_time(expires_at) <= now


def _tally_text(poll: dict, counts: dict[int, int], *, closed: bool) -> str:
    """票の数だけを書く。誰が投票したかは書かない。"""
    state = "（締め切りました）" if closed else ""
    lines = [f"📊 **投票の合計**（両方のサーバーの票）{state}", f"**{poll.get('question') or ''}**"]
    for answer in sorted(poll.get("answers") or [], key=lambda item: item["answer_id"]):
        lines.append(f"・{answer.get('text') or ''}：{counts.get(answer['answer_id'], 0)}票")
    return "\n".join(lines)[:rewrite.MAX_CONTENT]


def _thread_snapshot(info: dict) -> dict:
    return {"title": info.get("title"), "tags": info.get("tags") or "[]", "archived": int(info.get("archived") or 0), "locked": int(info.get("locked") or 0)}
