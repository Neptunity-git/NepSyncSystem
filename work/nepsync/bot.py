"""Discord との接続（discord.py）。同期の本体とスラッシュコマンドを、この1つの bot の中で動かす（ADR 0005・0010）。

- intents は GUILDS だけ（ADR 0010 判断2）。チャンネル・スレッド・ロールの形の知らせで、discord.py の手元の控えを保つ。
  発言・リアクション・メンバーの知らせは受けない。Message Content も使わない（人の情報は Neplector から読む）
- 同期の本体（engine.py）は、つながったあとに、この bot の流れの中で動く
- 答えの中身は commands.py が作り、返事はここで送る
- discord.py は DEBUG のログに送った中身（本文）を書くので、INFO より細かいログは出させない"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from typing import Any, Callable

import discord
from discord import app_commands

from .commands import CALL, DESCRIPTIONS, HIDE, MEMBERS, REACTORS, SHARE, SHARE_MENU, SHOW, VOTERS, WITHDRAW, WITHDRAW_MENU
from .roster import MAX_PICK, clean_ids, copy_embeds, mentions_text

BUTTON_PREFIX = "nepsync-members"   # ボタンの custom_id は「nepsync-members:チャンネル:ページ」。前から同じ形
PICK_PREFIX = "nepsync-pick"        # 名簿のメニュー「nepsync-pick:チャンネル:ページ:ページ数」。選んだ人の ID は values で届く
CLEAR_PREFIX = "nepsync-clear"      # 「選び直す」
STAR_PREFIX = "nepsync-star"        # 「★ に入れる・外す」
THREAD_TYPES = (discord.ChannelType.news_thread, discord.ChannelType.public_thread, discord.ChannelType.private_thread)
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}   # token の誤りなど。やり直しても直らない
MAX_PAUSE = 300.0
NO_PINGS = discord.AllowedMentions.none()
ANNOUNCE_POLL_SECONDS = 2.0   # 告知の依頼が終わったかを見に行く間隔
ANNOUNCE_POLLS = 45           # 最大で90秒待つ（Neplector に記録が届くのを待つことがある）


class FatalGatewayError(Exception):
    pass


def intents() -> discord.Intents:
    """GUILDS だけ。発言・リアクション・メンバーの知らせは受けない（ADR 0001 判断1・ADR 0010 判断2）。"""
    value = discord.Intents.none()
    value.guilds = True
    return value


class _Forward(logging.Handler):
    """discord.py のログを、こちらのログに流す。例外は種類だけ書く。"""

    def __init__(self, log: Callable[[str], None]) -> None:
        super().__init__(logging.INFO)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            text = str(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            text += f"（{type(record.exc_info[1]).__name__}）"
        self._log(f"discord.py {record.levelname}: {text}"[:300])


def quiet_library_logs(log: Callable[[str], None]) -> None:
    """discord.py のログを INFO 以上に限る。上（root）の設定が DEBUG でも、ここで止まる。"""
    logger = logging.getLogger("discord")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in [item for item in logger.handlers if isinstance(item, _Forward)]:
        logger.removeHandler(handler)
    logger.addHandler(_Forward(log))
    discord.VoiceClient.warn_nacl = discord.VoiceClient.warn_dave = False   # 音声は使わないので「音声の部品が無い」と言わせない


class Notifier:
    """運営者に知らせる（運営用の Webhook）。同じ知らせは1時間に1回。送れなくても同期は止めない。
    呼ぶ側は待たない（送るのは bot の流れの中で、あとから）。"""

    def __init__(self, url: str, log: Callable[[str], None]) -> None:
        self.url, self.log = url, log
        self.client: discord.Client | None = None
        self._sent: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    def __call__(self, text: str) -> None:
        self.log(text)
        clock = time.monotonic()
        if clock - self._sent.get(text, -3600) < 3600:
            return
        self._sent[text] = clock
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._send(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, text: str) -> None:
        if self.client is None or self.client.is_closed():
            return
        try:
            hook = discord.Webhook.from_url(self.url, client=self.client)
            await hook.send(f"[NepSync] {text}"[:2000], username="NepSync", allowed_mentions=NO_PINGS)
        except Exception as error:  # noqa: BLE001（知らせが送れなくても止めない）
            self.log(f"知らせを送れませんでした: {type(error).__name__}")


class PageButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_PREFIX + r":(?P<channel>\d+):(?P<page>-?\d+)"):
    """名簿の「前へ」「次へ」。custom_id の形で受け取るので、再起動の前に出したボタンも効く。"""

    def __init__(self, channel_id: str, page: int, *, label: str = "", disabled: bool = False) -> None:
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.secondary, label=label or None, disabled=disabled,
                                           custom_id=f"{BUTTON_PREFIX}:{channel_id}:{page}"))
        self.channel_id, self.page = str(channel_id), page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> "PageButton":
        return cls(match["channel"], int(match["page"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if isinstance(bot, SyncBot):
            await bot.guard(bot.turn_page(interaction, self.channel_id, self.page))


STATE = r":(?P<channel>\d+):(?P<page>\d+):(?P<pages>\d+)"   # 名簿の発言のどこにいるか。チャンネル 0 は名簿でない「呼ぶ」画面
MENTION = re.compile(r"<@!?(\d+)>")


def selected_in(message: Any) -> list[str]:
    """名簿の発言の本文にある <@ID> が、今選んでいる人（ADR 0014 判断4。手元に覚えないので、再起動の前の名簿でも効く）。"""
    return clean_ids(MENTION.findall(getattr(message, "content", None) or ""))


def menu_options(message: Any) -> list[dict]:
    """名簿の発言のメニューの選択肢（選び直すたびに Neplector に聞き直さない）。"""
    for row in getattr(message, "components", None) or []:
        for child in getattr(row, "children", []):
            if isinstance(child, discord.SelectMenu) and (child.custom_id or "").startswith(PICK_PREFIX):
                return [{"label": item.label, "value": item.value, "description": item.description} for item in child.options]
    return []


class PickMenu(discord.ui.DynamicItem[discord.ui.Select], template=PICK_PREFIX + STATE):
    """名簿の画像の下の「呼ぶ人を選ぶ」メニュー（ADR 0014 判断2・4）。選ぶと、名簿の発言の本文を書き換える。"""

    def __init__(self, channel_id: str, page: int, pages: int, options: list[dict] | None = None, selected: list[str] | None = None) -> None:
        chosen = set(selected or [])
        choices = [discord.SelectOption(label=item["label"], value=item["value"], description=item.get("description") or None,
                                        default=item["value"] in chosen)
                   for item in options or []] or [discord.SelectOption(label="-", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"{PICK_PREFIX}:{channel_id}:{page}:{pages}", placeholder=f"呼ぶ人を選ぶ（{MAX_PICK}人まで）",
                                           min_values=0, max_values=min(MAX_PICK, len(choices)), options=choices))
        self.channel_id, self.page, self.pages = str(channel_id), page, pages

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match: Any) -> "PickMenu":
        return cls(match["channel"], int(match["page"]), int(match["pages"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        values = [str(item) for item in (interaction.data or {}).get("values", [])]   # type: ignore[union-attr]
        if isinstance(bot, SyncBot):
            await bot.guard(bot.choose(interaction, self.channel_id, self.page, self.pages, values))


class ClearButton(discord.ui.DynamicItem[discord.ui.Button], template=CLEAR_PREFIX + STATE):
    """「選び直す」：選んだ人を空にする。"""

    def __init__(self, channel_id: str, page: int, pages: int) -> None:
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.secondary, label="選び直す",
                                           custom_id=f"{CLEAR_PREFIX}:{channel_id}:{page}:{pages}"))
        self.channel_id, self.page, self.pages = str(channel_id), page, pages

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> "ClearButton":
        return cls(match["channel"], int(match["page"]), int(match["pages"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if isinstance(bot, SyncBot):
            await bot.guard(bot.reselect(interaction, self.channel_id, self.page, self.pages, []))


class StarButton(discord.ui.DynamicItem[discord.ui.Button], template=STAR_PREFIX + STATE):
    """「★ に入れる・外す」：1人だけ選んでいるとき（ADR 0014 判断3）。"""

    def __init__(self, channel_id: str, page: int, pages: int, starred: bool = False) -> None:
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.secondary, label="★ から外す" if starred else "★ に入れる（名簿の先頭に出す）",
                                           custom_id=f"{STAR_PREFIX}:{channel_id}:{page}:{pages}"))
        self.channel_id, self.page, self.pages = str(channel_id), page, pages

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> "StarButton":
        return cls(match["channel"], int(match["page"]), int(match["pages"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if isinstance(bot, SyncBot):
            await bot.toggle_star(interaction, self.channel_id, self.page, self.pages)


def page_buttons(pager: dict | None, view: discord.ui.View | None = None) -> discord.ui.View | None:
    if not pager:
        return view
    view = view or discord.ui.View(timeout=None)
    page, pages, channel_id = pager["page"], pager["pages"], pager["channel_id"]
    view.add_item(PageButton(channel_id, page - 1, label="前へ", disabled=page == 0))
    view.add_item(PageButton(channel_id, page + 1, label="次へ", disabled=page >= pages - 1))
    return view


def selection_view(channel_id: str, page: int, pages: int, options: list[dict], selected: list[str], star: bool | None) -> discord.ui.View | None:
    """名簿の発言の部品：メニュー、その下に「前へ」「次へ」「選び直す」「★」。名簿でない「呼ぶ」画面は ★ だけ。"""
    view = discord.ui.View(timeout=None)
    if options:
        view.add_item(PickMenu(channel_id, page, pages, options, selected))
    if pages > 1:
        view.add_item(PageButton(channel_id, page - 1, label="前へ", disabled=page == 0))
        view.add_item(PageButton(channel_id, page + 1, label="次へ", disabled=page >= pages - 1))
    if options and selected:
        view.add_item(ClearButton(channel_id, page, pages))
    if star is not None and len(selected) == 1:
        view.add_item(StarButton(channel_id, page, pages, star))
    return view if view.children else None


class SyncBot(discord.Client):
    """engine（同期の本体）と logic（コマンドの答え）は、作ったあとで入れる（どちらもこの bot を使うため）。
    run_engine=False は doctor・plan のように、つないで見るだけのとき。"""

    def __init__(self, config: Any, *, notify: Callable[[str], None], log: Callable[[str], None],
                 commands: bool = True, run_engine: bool = True) -> None:
        super().__init__(intents=intents())
        self.config, self.notify, self.log = config, notify, log
        self.run_engine = run_engine
        self.engine: Any = None
        self.logic: Any = None
        self.neplector: Any = None
        self._engine_task: asyncio.Task | None = None
        self.guild_objects = [discord.Object(id=int(guild_id)) for guild_id in (config.guild_a, config.guild_b)]
        self.tree = app_commands.CommandTree(self)
        self.commands_on = commands and config.slash_commands
        if self.commands_on:
            self._add_commands()
            self.add_dynamic_items(PageButton, PickMenu, ClearButton, StarButton)

    def _add_commands(self) -> None:
        async def suggest(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            if not self.logic.in_scope(interaction.guild_id):
                return []
            found = await self.logic.suggest(channel_of(interaction), str(interaction.guild_id), str(interaction.user.id), current)
            return [app_commands.Choice(name=name, value=value) for name, value in found]

        @app_commands.rename(query="名前")
        @app_commands.describe(query="呼びたい人の名前。打ち始めると候補が出ます（空なら一覧）")
        @app_commands.autocomplete(query=suggest)
        async def members(interaction: discord.Interaction, query: str | None = None) -> None:
            await self.guard(self.show_members(interaction, query))

        async def hide(interaction: discord.Interaction) -> None:
            await self.guard(self.set_visibility(interaction, hide=True))

        async def show(interaction: discord.Interaction) -> None:
            await self.guard(self.set_visibility(interaction, hide=False))

        async def reactors(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.guard(self.show_list(interaction, message, self.logic.reactors))

        async def voters(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.guard(self.show_list(interaction, message, self.logic.voters))

        async def call(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.guard(self.call_author(interaction, message))

        for name, callback in ((MEMBERS, members), (HIDE, hide), (SHOW, show)):
            self.tree.add_command(app_commands.Command(name=name, description=DESCRIPTIONS[name], callback=callback),
                                  guilds=self.guild_objects)
        for name, callback in ((REACTORS, reactors), (VOTERS, voters), (CALL, call)):   # 発言の右クリックのメニューは、1つのサーバーに5つまで
            self.tree.add_command(app_commands.ContextMenu(name=name, callback=callback), guilds=self.guild_objects)
        if self.config.announcements:   # 告知の組があるときだけ、メニューを出す（ADR 0009）
            async def share(interaction: discord.Interaction, message: discord.Message) -> None:
                await self.guard(self.handle_announce(interaction, message, SHARE))

            async def withdraw(interaction: discord.Interaction, message: discord.Message) -> None:
                await self.guard(self.handle_announce(interaction, message, WITHDRAW))

            for name, callback in ((SHARE_MENU, share), (WITHDRAW_MENU, withdraw)):
                self.tree.add_command(app_commands.ContextMenu(name=name, callback=callback), guilds=self.guild_objects)

    async def guard(self, job: Any) -> None:
        """1回の失敗で接続を切らない。詳しいことはログに書かない。"""
        try:
            await job
        except Exception as error:  # noqa: BLE001
            self.log(f"コマンドに答えられませんでした: {type(error).__name__}")

    async def setup_hook(self) -> None:
        if self.commands_on:
            await self.register()

    async def on_ready(self) -> None:
        """つながって、サーバーの控えがそろった。同期の本体を始める（つなぎ直しのたびに呼ばれるので、1回だけ）。"""
        if self.run_engine and self.engine is not None and self._engine_task is None:
            self._engine_task = asyncio.create_task(self.engine.run_forever(), name="nepsync-engine")

    async def close(self) -> None:
        if self._engine_task is not None:
            self._engine_task.cancel()
            await asyncio.gather(self._engine_task, return_exceptions=True)
        if self.neplector is not None:
            await self.neplector.close()
        await super().close()

    async def register(self) -> bool:
        """両方のサーバーにコマンドを登録する（今あるものと入れ替える）。失敗したら知らせる。"""
        ok = True
        for guild in self.guild_objects:
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException as error:
                ok = False
                self.notify(f"サーバー {guild.id} にスラッシュコマンドを登録できません（{error.status} code={error.code}）。"
                            "bot を applications.commands 付きで招待し直してください")
        return ok

    async def answer(self, interaction: discord.Interaction, body: dict) -> None:
        await interaction.edit_original_response(content=body["content"], embeds=[discord.Embed.from_dict(item) for item in body["embeds"]],
                                                 view=page_buttons(body.get("pager")), allowed_mentions=NO_PINGS)

    async def answer_roster(self, interaction: discord.Interaction, body: dict, channel_id: str = "") -> None:
        """名簿の答え（roster.py の形）。page は画像とメニュー、pick は <@ID> だけの本文とボタン、text は文だけ。"""
        if body["kind"] == "page":
            picture = discord.File(io.BytesIO(body["image"]), filename="members.png")
            view = selection_view(channel_id, body["page"], body["pages"], body["options"], body["selected"], body["star"])
            await interaction.edit_original_response(content=body["content"] or None, embeds=[discord.Embed.from_dict(item) for item in body["embeds"]],
                                                     attachments=[picture], view=view, allowed_mentions=NO_PINGS)
        elif body["kind"] == "pick":
            view = selection_view("0", 0, 1, [], body["selected"], body["star"])
            await interaction.edit_original_response(content=body["content"], embeds=[discord.Embed.from_dict(item) for item in body["embeds"]],
                                                     attachments=[], view=view, allowed_mentions=NO_PINGS)
        else:
            await interaction.edit_original_response(content=body["content"], embeds=[], attachments=[], view=None, allowed_mentions=NO_PINGS)

    async def show_members(self, interaction: discord.Interaction, query: str | None = None) -> None:
        if not self.logic.in_scope(interaction.guild_id):
            return
        channel_id = channel_of(interaction)
        # 3秒以内に答える決まりなので、先に「考え中」を返す（打った人にだけ見える）
        await interaction.response.defer(ephemeral=True, thinking=True)
        body = await self.logic.members(channel_id, 0, str(interaction.guild_id), str(interaction.user.id), query)
        await self.answer_roster(interaction, body, channel_id)

    async def turn_page(self, interaction: discord.Interaction, channel_id: str, page: int) -> None:
        if not self.logic.in_scope(interaction.guild_id):
            return
        await interaction.response.defer()   # ボタンなので「今の発言を書き換える」待ち
        selected = selected_in(interaction.message)
        body = await self.logic.members(channel_id, page, str(interaction.guild_id), str(interaction.user.id), selected=selected)
        await self.answer_roster(interaction, body, channel_id)

    async def choose(self, interaction: discord.Interaction, channel_id: str, page: int, pages: int, values: list[str]) -> None:
        """名簿のメニューで選んだ。新しい返事は出さず、名簿の発言の本文を、選んだ人の <@ID> に書き換える。
        ほかのページで選んだ人は残し、このページの人は、メニューで今選ばれている人だけにする。"""
        if not self.logic.in_scope(interaction.guild_id):
            return
        on_page = {item["value"] for item in menu_options(interaction.message)}
        current = selected_in(interaction.message)
        chosen = clean_ids([item for item in current if item not in on_page or item in values] + values)
        self.logic.roster.remember(str(interaction.user.id), [item for item in chosen if item not in current])
        await self.reselect(interaction, channel_id, page, pages, chosen)

    async def reselect(self, interaction: discord.Interaction, channel_id: str, page: int, pages: int, chosen: list[str]) -> None:
        """名簿の発言を、選んだ人に合わせて書き換える。本文は <@ID>（スマホの長押し用）、下の枠はコードブロック（PC 用）。画像はそのまま。"""
        if not self.logic.in_scope(interaction.guild_id):
            return
        star = self.logic.roster.starred(str(interaction.user.id), chosen[0]) if len(chosen) == 1 else None
        view = selection_view(channel_id, page, pages, menu_options(interaction.message), chosen, star)
        embeds = [discord.Embed.from_dict(item) for item in copy_embeds(chosen)]
        await interaction.response.edit_message(content=mentions_text(chosen) or None, embeds=embeds, view=view, allowed_mentions=NO_PINGS)

    async def toggle_star(self, interaction: discord.Interaction, channel_id: str, page: int, pages: int) -> None:
        """選んでいる1人を ★ に入れる・外す。"""
        chosen = selected_in(interaction.message)
        if len(chosen) == 1 and self.logic.in_scope(interaction.guild_id):
            user_id = str(interaction.user.id)
            self.logic.roster.set_star(user_id, chosen[0], not self.logic.roster.starred(user_id, chosen[0]))
        await self.guard(self.reselect(interaction, channel_id, page, pages, chosen))

    async def call_author(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """写しの長押しのメニュー「この人を呼ぶ」。使うのは発言の ID だけ（本文は使わない）。"""
        if not self.logic.in_scope(interaction.guild_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        body = await self.logic.call_author(str(message.id), str(interaction.guild_id), str(interaction.user.id))
        await self.answer_roster(interaction, body)

    async def show_list(self, interaction: discord.Interaction, message: discord.Message, make: Callable[[str, str], Any]) -> None:
        """「リアクションした人」「投票した人」。押した人にだけ見せる。使うのは発言の ID だけ（本文は使わない）。"""
        if not self.logic.in_scope(interaction.guild_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.answer(interaction, await make(str(message.id), str(interaction.guild_id)))

    async def handle_announce(self, interaction: discord.Interaction, message: discord.Message, action: str) -> None:
        """告知を出す・下げる。message には本文も入っているが、使うのは ID と書いた人の ID だけ（ADR 0009 判断2）。
        依頼を控えに書き、同期の本体にすぐ処理させる。Neplector に記録が届くのを待つときは、少しずつ見に行く。"""
        if not self.logic.in_scope(interaction.guild_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        can_manage = bool(interaction.permissions.manage_messages)
        text = self.logic.announce(action, str(message.id), str(interaction.channel_id),
                                   str(interaction.user.id), str(message.author.id), can_manage)
        if text is None and self.engine is not None:
            await self.engine.announce_now()
            text = self.logic.announce_result(str(message.id))
        for _ in range(ANNOUNCE_POLLS if text is None else 0):
            await asyncio.sleep(ANNOUNCE_POLL_SECONDS)
            text = self.logic.announce_result(str(message.id))
            if text is not None:
                break
        await interaction.edit_original_response(content=text or "受け付けました。まだ終わっていません。少ししてから相手のサーバーを確かめてください",
                                                 allowed_mentions=NO_PINGS)

    async def set_visibility(self, interaction: discord.Interaction, *, hide: bool) -> None:
        if not self.logic.in_scope(interaction.guild_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = self.logic.visibility(str(interaction.user.id), hide=hide)
        await interaction.edit_original_response(content=text or "あなたを確かめられませんでした", allowed_mentions=NO_PINGS)


def channel_of(interaction: discord.Interaction) -> str:
    """打たれたチャンネル。スレッドの中なら、その親のチャンネル。"""
    channel = interaction.channel
    parent = getattr(channel, "parent_id", None) if getattr(channel, "type", None) in THREAD_TYPES else None
    return str(parent or interaction.channel_id)


async def _session(token: str, build: Callable[[], SyncBot]) -> None:
    async with build() as client:
        await client.start(token)   # 切れたら discord.py がつなぎ直す。戻るのは直せないとき


def run_forever(token: str, build: Callable[[], SyncBot], *, log: Callable[[str], None],
                session: Callable[..., Any] = _session, sleep: Callable[[float], None] = time.sleep) -> None:
    """接続を保ち続ける。切れたら bot を作り直してつなぎ直す。token の誤りのように直らないときは FatalGatewayError。"""
    quiet_library_logs(log)
    pause = 5.0
    while True:
        started = time.monotonic()
        try:
            asyncio.run(session(token, build))
        except discord.LoginFailure as error:
            raise FatalGatewayError("login") from error
        except discord.PrivilegedIntentsRequired as error:
            raise FatalGatewayError("intents") from error
        except Exception as error:  # noqa: BLE001（止めずにつなぎ直す）
            if getattr(error, "code", None) in FATAL_CLOSE_CODES:
                raise FatalGatewayError(f"close code {error.code}") from error   # type: ignore[attr-defined]
            log(f"Discord との接続が切れました（{type(error).__name__}）。{pause:.0f} 秒後につなぎ直します")
        if time.monotonic() - started > 60:
            pause = 5.0
        sleep(pause)
        pause = min(pause * 2, MAX_PAUSE)


async def connected(token: str, client: SyncBot) -> None:
    """つないで、サーバーの控えがそろうまで待つ（doctor・plan などの1回きりの仕事のため）。
    終わったら呼ぶ側が client.close() を呼ぶ。"""
    await client.login(token)
    runner = asyncio.create_task(client.connect(reconnect=False))
    ready = asyncio.create_task(client.wait_until_ready())
    done, _ = await asyncio.wait({runner, ready}, return_when=asyncio.FIRST_COMPLETED)
    if runner in done:
        ready.cancel()
        runner.result()   # つなげなかった理由を、そのまま上に出す
        raise FatalGatewayError("disconnected")
