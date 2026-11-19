"""スラッシュコマンドの答えの中身（ADR 0005・0007・0008）。

- /nepsync-members：相手のサーバーの人を、アイコンの格子で打った人にだけ見せ、選んだ人を呼ぶ文字を出す（中身は roster.py。ADR 0014）
- 写しの長押しのメニュー「この人を呼ぶ」：その写しを書いた人を呼ぶ文字を出す（ADR 0014 判断6）
- /nepsync-hide・/nepsync-show：自分を名簿に載せない・載せるに戻す
- 発言の右クリック →「リアクションした人」：両方のサーバーで誰がリアクションしたかを、押した人にだけ見せる（ADR 0004 判断6）
- 発言の右クリック →「投票した人」：両方のサーバーで、選択肢ごとに誰が投票したかを、押した人にだけ見せる（ADR 0011 判断2）
- 発言の右クリック →「相手のサーバーにも出す」「相手のサーバーから下げる」：告知チャンネルの発言を出す・下げる（ADR 0009）。
  依頼を SQLite に書くだけで、出すのは同期の本体。メニューと一緒に届く本文は使わない・残さない

ここは答えの中身（文・埋め込み・ページ）を作るだけで、Discord への返事は bot.py（discord.py）が送る。
サーバーの名前・ロール・チャンネルの権限の上書きは、discord.py の手元の控え（GUILDS の知らせ）から読む。
外れた人は Neplector の名簿に無いので、ここにも出ない。bot は載せない。"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import discord

from .config import Config
from .neplector import NeplectorError, Transient
from .roster import Roster, text_only

FAILURES = (discord.HTTPException, NeplectorError, Transient, LookupError)

MEMBERS, HIDE, SHOW = "nepsync-members", "nepsync-hide", "nepsync-show"
REACTORS = "リアクションした人"
CALL = "この人を呼ぶ"
VOTERS = "投票した人"
SHARE_MENU, WITHDRAW_MENU = "相手のサーバーにも出す", "相手のサーバーから下げる"
SHARE, WITHDRAW = "share", "withdraw"   # engine.py と同じ記号
ANNOUNCE_RESULTS = {
    "shared": "相手のサーバーに出しました",
    "withdrawn": "相手のサーバーから下げました。元の告知は残っています",
    "not_shared": "この告知は相手のサーバーに出ていません",
    "is_copy": "これは相手のサーバーから来た写しです。出すときは元の告知で押してください",
    "not_announcement": "このメニューは、告知チャンネルの発言（スレッドの中は除く）にだけ使えます",
    "direction": "今の設定では、このサーバーから相手には出しません",
    "no_record": "Neplector にこの発言の記録が無いので出せません（このチャンネルを Neplector が読んでいないか、記録から外れた人の発言です）",
    "no_body": "この発言は本文が取れないので出せません（本文を開く日より前の発言か、bot の投稿です）",
    "failed": "出せませんでした。運営者に知らせました",
}
DESCRIPTIONS = {
    MEMBERS: "相手のサーバーの人を一覧にして、呼ぶための文字を自分にだけ表示します",
    HIDE: "/nepsync-members の名簿に、自分を載せないようにします",
    SHOW: "/nepsync-members の名簿に、自分を載せるように戻します",
}
MAX_TEXT = 2000


DEAD_LINK_NOTES = {
    "message": "発言が同期されていないので、相手のサーバーではチャンネルへのリンクにしました",
    "channel": "同期していないチャンネルなので、元のサーバーのリンクのままです。相手のサーバーの人は開けません",
}
MAX_DEAD_LINKS = 10


def dead_link_warning(links: list) -> str:
    """相手のサーバーで開けないリンクの警告（ADR 0009 判断5）。リンクは展開されないように <> で囲む。"""
    lines = [f"⚠️ 相手のサーバーで、指している発言を開けないリンクが {len(links)} 個あります"]
    lines.extend(f"・<{url}>\n　{DEAD_LINK_NOTES.get(kind, '')}" for url, kind in links[:MAX_DEAD_LINKS])
    if len(links) > MAX_DEAD_LINKS:
        lines.append(f"・ほか {len(links) - MAX_DEAD_LINKS} 個")
    lines.append("直すときは「相手のサーバーから下げる」で下げるか、元の告知を編集してください（編集は写しにも届きます）")
    return "\n".join(lines)


def only(text: str) -> dict:
    """文だけの答え。"""
    return {"content": text, "embeds": [], "pager": None}


class Commands:
    """答えは {"content": 文, "embeds": 埋め込みの辞書の並び, "pager": None か {"channel_id", "page", "pages"}}。"""

    def __init__(self, config: Config, neplector: Any, client: Any, store: Any, *,
                 avatars: Any = None, log: Callable[[str], None] = print, clock: Callable[[], float] = time.time) -> None:
        self.config, self.nep, self.client, self.store = config, neplector, client, store
        self.avatars, self.log, self.clock = avatars, log, clock
        self.roster = Roster(config, neplector, client, store, log=log, clock=clock)

    def pairs(self) -> list[tuple[str, str]]:
        return [(row["a_id"], row["b_id"]) for row in self.store.channel_pairs()]

    def pair_of(self, message_id: str) -> dict | None:
        return self.store.message_by_source(message_id) or self.store.message_by_copy(message_id)

    def guild(self, guild_id: str) -> Any:
        """手元の控えのサーバー。まだ無ければ LookupError（答えられないと返す）。"""
        found = self.client.get_guild(int(guild_id))
        if found is None or found.unavailable:
            raise LookupError(guild_id)
        return found

    def in_scope(self, guild_id: Any) -> bool:
        """同期している2つのサーバーのどちらかか。"""
        return str(guild_id) in (self.config.guild_a, self.config.guild_b)

    async def _roster(self, job: Any) -> dict:
        """名簿の答え。作れないときは、詳しいことを書かずに知らせる。"""
        try:
            return await job
        except FAILURES as error:
            self.log(f"名簿を作れませんでした: {type(error).__name__}")
            return text_only("名簿を作れませんでした。少し待ってからやり直してください")

    async def members(self, channel_id: str, page: int, guild_id: str, user_id: str, query: str | None = None,
                      selected: list[str] | None = None) -> dict:
        """名簿の1ページか、名前で探した答え（roster.py の形）。selected は、前に選んでいた人（ページ送りで残す）。"""
        if query and query.strip():
            return await self._roster(self.roster.search(channel_id, guild_id, user_id, query.strip()))
        return await self._roster(self.roster.page(channel_id, page, guild_id, user_id, selected=selected))

    async def pick(self, guild_id: str, user_id: str, targets: list[str]) -> dict:
        return await self._roster(self.roster.pick(guild_id, user_id, targets))

    async def call_author(self, message_id: str, guild_id: str, user_id: str) -> dict:
        return await self._roster(self.roster.call_author(message_id, guild_id, user_id))

    async def suggest(self, channel_id: str, guild_id: str, user_id: str, query: str) -> list[tuple[str, str]]:
        """入力候補。3秒以内に答える決まりなので、作れなければ空にする。"""
        try:
            return await self.roster.suggest(channel_id, guild_id, user_id, query)
        except FAILURES as error:
            self.log(f"名簿の入力候補を作れませんでした: {type(error).__name__}")
            return []

    async def reactors(self, message_id: str, guild_id: str) -> dict:
        try:
            return await self.reactors_body(message_id, guild_id)
        except FAILURES as error:
            self.log(f"リアクションの一覧を作れませんでした: {type(error).__name__}")
            return only("一覧を作れませんでした。少し待ってからやり直してください")

    async def voters(self, message_id: str, guild_id: str) -> dict:
        try:
            return await self.voters_body(message_id, guild_id)
        except FAILURES as error:
            self.log(f"投票した人の一覧を作れませんでした: {type(error).__name__}")
            return only("一覧を作れませんでした。少し待ってからやり直してください")

    def visibility(self, user_id: str, *, hide: bool) -> str | None:
        """名簿に載せる・載せないを保存して、答えの文を返す。人が分からないときは None。"""
        if not str(user_id).isdigit():
            return None
        try:
            self.store.set_hidden(str(user_id), hide)
        except Exception as error:  # noqa: BLE001（答えずに終わらない）
            self.log(f"名簿の設定を保存できませんでした: {type(error).__name__}")
            return "設定を保存できませんでした。少し待ってからやり直してください"
        if hide:
            return ("名簿（/nepsync-members）に載らないようにしました。戻すときは /nepsync-show を実行してください。"
                    "発言の同期やメンションは、これまでどおりです")
        return "名簿（/nepsync-members）に載るように戻しました"

    def announce(self, action: str, message_id: str, channel_id: str, user_id: str, author_id: str, can_manage: bool) -> str | None:
        """告知を出す・下げる依頼を受ける。すぐ答えられるときは答えの文、依頼を書いたときは None（あとで announce_result で聞く）。
        使えるのは、そのチャンネルで「メッセージの管理」を持つ人と、告知を書いた本人（写しなら元の告知を書いた人）。"""
        if self.config.announce_side(channel_id) is None:
            return ANNOUNCE_RESULTS["not_announcement"]
        pair = self.pair_of(message_id)
        writer = pair["author_id"] if pair and pair["copy_id"] == message_id else author_id
        if not can_manage and user_id != writer:
            return "使えるのは、このチャンネルで「メッセージの管理」を持つ人と、告知を書いた本人だけです"
        if action == SHARE and pair and pair["copy_id"] == message_id:
            return ANNOUNCE_RESULTS["is_copy"]
        if action == WITHDRAW and pair is None:
            return ANNOUNCE_RESULTS["not_shared"]
        try:
            self.store.request_announce(message_id, channel_id, action, self.clock())
        except Exception as error:  # noqa: BLE001（答えずに終わらない）
            self.log(f"告知の依頼を保存できませんでした: {type(error).__name__}")
            return "依頼を保存できませんでした。少し待ってからやり直してください"
        return None

    def announce_result(self, message_id: str) -> str | None:
        """依頼が終わっていれば答えの文。まだなら None。"""
        row = self.store.announce_request(message_id)
        if row is None or row["result"] is None:
            return None
        text = ANNOUNCE_RESULTS.get(row["result"], ANNOUNCE_RESULTS["failed"])
        if row.get("link"):
            text += f"\n{row['link']}"
        if row.get("warning"):
            text += "\n\n" + dead_link_warning(json.loads(row["warning"]))
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"

    async def reactors_body(self, message_id: str, guild_id: str) -> dict:
        """元の発言と写しの両方のリアクションを、絵文字ごとにまとめ、その下にサーバーごとの名前を並べる。
        カスタム絵文字は名前でまとめ、押した人のサーバーで表示できる形にする（ID はサーバーごとに違う）。"""

        pair = self.pair_of(message_id)
        if pair is None:
            return only("この発言は同期していないので、相手のサーバーのリアクションはありません")
        found = await self.nep.messages([pair["source_id"], pair["copy_id"]])
        if pair["source_id"] not in found:   # 外れた人の発言か、消された発言
            return only("この発言のリアクションは表示できません")
        names: dict[tuple[str, str], str] = {}
        groups: dict[str, dict[str, list[str]]] = {}
        shown: dict[str, str] = {}
        for message in sorted(found.values(), key=lambda item: str(item.get("guild_id")) != guild_id):   # 押した人のサーバーを先に
            guild = str(message.get("guild_id"))
            for reaction in message.get("reactions") or []:
                emoji = reaction["emoji"]
                key = emoji.split(":")[0] if ":" in emoji else emoji
                if key not in shown:
                    shown[key] = await self._emoji_text(emoji, guild_id)
                name = await self._display(guild, reaction["user_id"], names)
                group = groups.setdefault(key, {}).setdefault(guild, [])
                if name not in group:
                    group.append(name)
        if not groups:
            return only("まだ、どちらのサーバーでもリアクションがありません")
        guild_names = {guild: self.guild(guild).name for guild in {g for item in groups.values() for g in item}}
        lines = ["**リアクションした人**（両方のサーバー）"]
        for key, by_guild in groups.items():
            lines.append(f"{shown[key]}　{sum(len(people) for people in by_guild.values())}人")
            lines.extend(f"　{guild_names[guild]}：{'・'.join(people)}" for guild, people in by_guild.items())
        text = "\n".join(lines)
        return only(text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…")

    async def voters_body(self, message_id: str, guild_id: str) -> dict:
        """投票と、その写しの票を、選択肢ごとにまとめ、その下にサーバーごとの名前を並べる。
        「投票の合計」の発言を右クリックしても、その投票の一覧を出す。人数は合計の発言と同じ数え方（同じ人の同じ選択肢は1人）。"""
        pair = self.pair_of(message_id)
        if pair is None:
            tally = self.store.tally_by_message(message_id)
            pair = self.store.message_by_source(tally["source_id"]) if tally else None
        if pair is None:
            return only("この発言は同期している投票ではありません")
        found = await self.nep.messages([pair["source_id"], pair["copy_id"]])
        original = found.get(pair["source_id"])
        if original is None:   # 外れた人の投票か、消された投票
            return only("この投票は表示できません")
        if not original.get("poll"):
            return only("この発言は投票ではありません")
        names: dict[tuple[str, str], str] = {}
        people: dict[int, set[str]] = {}
        groups: dict[int, dict[str, list[str]]] = {}
        for message in sorted(found.values(), key=lambda item: str(item.get("guild_id")) != guild_id):   # 押した人のサーバーを先に
            guild = str(message.get("guild_id"))
            for vote in (message.get("poll") or {}).get("votes") or []:
                people.setdefault(vote["answer_id"], set()).add(vote["user_id"])
                name = await self._display(guild, vote["user_id"], names)
                group = groups.setdefault(vote["answer_id"], {}).setdefault(guild, [])
                if name not in group:
                    group.append(name)
        if not groups:
            return only("まだ、どちらのサーバーでも投票がありません")
        guild_names = {guild: self.guild(guild).name for guild in {g for item in groups.values() for g in item}}
        lines = ["**投票した人**（両方のサーバー）", f"**{original['poll'].get('question') or ''}**"]
        for answer in sorted(original["poll"].get("answers") or [], key=lambda item: item["answer_id"]):
            emoji = await self._emoji_text(answer["emoji"], guild_id) + " " if answer.get("emoji") else ""
            lines.append(f"{emoji}{answer.get('text') or ''}　{len(people.get(answer['answer_id'], ()))}人")
            lines.extend(f"　{guild_names[guild]}：{'・'.join(voted)}" for guild, voted in groups.get(answer["answer_id"], {}).items())
        text = "\n".join(lines)
        return only(text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…")

    async def _display(self, guild: str, user_id: str, names: dict[tuple[str, str], str]) -> str:
        """一覧に出す名前（アイコンの絵文字＋名前）。名簿に無い人は「メンバー」。"""
        person = (guild, user_id)
        if person not in names:
            rows = await self.nep.table("member_servers", guild_id=guild, user_id=user_id)
            row = rows[0] if rows else {}
            icon = await self.avatars.emoji_for(guild, user_id, row.get("avatar_url")) if self.avatars and row else ""
            names[person] = icon + (row.get("display_name") or "メンバー")
        return names[person]

    async def _emoji_text(self, emoji: str, guild_id: str) -> str:
        """押した人のサーバーで表示できる形。無いカスタム絵文字は :名前: にする。"""
        if ":" not in emoji:
            return emoji
        name = emoji.split(":")[0]
        rows = [row for row in await self.nep.table("emojis", guild_id=guild_id, name=name) if row.get("available")]
        if not rows:
            return f":{name}:"
        return f"<{'a' if rows[0].get('animated') else ''}:{name}:{rows[0]['emoji_id']}>"
