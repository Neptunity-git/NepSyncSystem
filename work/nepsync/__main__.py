"""コマンド：doctor（確かめる）・plan（することを見るだけ）・run（動かす）・approve-deletes（止めた削除を進める）。

どれも discord.py で Discord につないで、サーバーの控え（GUILDS の知らせ）がそろってから動く。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import A_TO_B, ROOT, ConfigError, load_config, load_secrets
from .store import Store

PERMS = {"View Channel": 1 << 10, "Manage Channels": 1 << 4, "Manage Webhooks": 1 << 29, "Create Public Threads": 1 << 35,
         "Manage Threads": 1 << 34, "Send Messages in Threads": 1 << 38, "Read Message History": 1 << 16, "Add Reactions": 1 << 6,
         "Pin Messages": 1 << 51}   # ピン留め（ADR 0012）。「メッセージの管理」とは別の権限
ANNOUNCE_PERMS = ("View Channel", "Manage Webhooks", "Read Message History", "Add Reactions")
MENTION_EVERYONE = 1 << 17


class AlreadyRunning(Exception):
    pass


def hold_lock(path: Path) -> sqlite3.Connection:
    """同じフォルダで2つ動くと発言が二重に流れるので、鍵を持ち続ける。プロセスが終われば外れる（Windows でも Linux でも同じ）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as error:
        connection.close()
        raise AlreadyRunning(str(path)) from error
    return connection


def _stamped(text: str) -> None:
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {text}", flush=True)


def build(config: Any, secrets: Any, store: Store, notify: Callable[[str], None], *, commands: bool = True, run_engine: bool = True) -> Any:
    """bot と、その中で動く部品をそろえる。つなぎ直すたびに作り直す（aiohttp のつなぎは、動いている流れごとに作る決まりなので）。"""
    from .avatars import AvatarEmojis
    from .bot import SyncBot
    from .commands import Commands
    from .engine import Engine
    from .neplector import Neplector

    client = SyncBot(config, notify=notify, log=_stamped, commands=commands, run_engine=run_engine)
    neplector = Neplector(secrets.neplector_base_url, secrets.neplector_api_key)
    engine = Engine(config, neplector, client, store, notify=notify, download=neplector.download, log=_stamped)
    avatars = None
    if config.slash_commands:
        avatars = AvatarEmojis(client, store, {config.guild_a: "a", config.guild_b: "b"}, neplector.download, _stamped,
                               keep_days=config.avatar_emoji_days)
    engine.avatars = avatars
    client.engine, client.neplector = engine, neplector
    client.logic = Commands(config, neplector, client, store, avatars=avatars, log=_stamped)
    if hasattr(notify, "client"):
        notify.client = client
    return client


def _once(work: Callable[[Any, Any], Awaitable[int]]) -> int:
    """つないで1回だけ仕事をして、切る。"""
    from . import bot

    config, secrets = load_config(), load_secrets()
    store = Store(ROOT / "data" / "nepsync.db")
    bot.quiet_library_logs(_stamped)

    async def main() -> int:
        client = build(config, secrets, store, _stamped, commands=False, run_engine=False)
        try:
            await bot.connected(secrets.discord_bot_token, client)
            return await work(config, client)
        finally:
            await client.close()
    return asyncio.run(main())


async def doctor(config: Any, client: Any) -> int:
    problems = 0

    def check(ok: bool, text: str) -> None:
        nonlocal problems
        print(f"{'OK' if ok else 'NG'}  {text}")
        problems += 0 if ok else 1

    from .neplector import NeplectorError
    neplector = client.neplector
    try:
        check(await neplector.latest_seq() >= 0, "Neplector の API キー")
    except NeplectorError as error:
        check(False, f"Neplector の API キー: {error}")
    print(f"向き: {config.direction}（{'a → b の一方通行。b には読みに行かず、a には書きません' if config.direction == A_TO_B else '双方向'}）")
    for name in config.category_names:
        for side, guild_id in (("a", config.guild_a), ("b", config.guild_b)):
            writable = config.direction != A_TO_B or side == "b"
            guild = client.get_guild(int(guild_id))
            check(guild is not None and not guild.unavailable, f"bot が {side}（{guild_id}）のサーバーに入っている")
            if guild is None or guild.unavailable:
                continue
            found = [item for item in guild.categories if item.name == name]
            check(len(found) == 1, f"{side}（{guild_id}）にカテゴリ「{name}」が1つある")
            if len(found) != 1:
                continue
            category = found[0]
            allow = category.permissions_for(guild.me).value
            needed = PERMS if writable else {"View Channel": PERMS["View Channel"]}
            for perm, bit in needed.items():
                check(bool(allow & bit), f"  bot の権限: {perm}")
            if not writable:
                extra = [perm for perm, bit in PERMS.items() if perm != "View Channel" and allow & bit]
                check(not extra, f"  書かない側なので余分な権限が無い: {extra or 'なし'}")
            everyone_allow = category.overwrites_for(guild.default_role).pair()[0].value
            if everyone_allow & (PERMS["Manage Channels"] | PERMS["Manage Webhooks"] | PERMS["Manage Threads"]):
                print("注意  このカテゴリでは @everyone に管理の権限が付いています。全員がチャンネルや Webhook を変更・削除できます")
            rows = await neplector.table("channels", category_id=str(category.id))
            reading = [row for row in rows if row.get("reading") == 1]
            if config.flows_from(side):
                if category.channels:
                    check(bool(reading), f"  Neplector がこのカテゴリを読んでいる（{len(reading)} チャンネル）")
                else:
                    print("情報  カテゴリは空です。チャンネルができたら、Neplector が読んでいるかをもう一度確かめてください")
                for child in category.channels:
                    check(child.permissions_for(guild.me).view_channel, f"  bot がチャンネル {child.name} を見られる")
    for pair in config.announcements:
        await _check_announcements(config, client, pair, check)
    print("問題なし" if not problems else f"問題 {problems} 件")
    return 1 if problems else 0


async def _check_announcements(config: Any, client: Any, pair: tuple[str, str], check: Callable[[bool, str], None]) -> None:
    """告知チャンネルの組（ADR 0009）。写しを書くための権限と、Neplector が元を読んでいるか、通知するロールがあるか。"""
    for side, channel_id in zip(("a", "b"), pair):
        guild_id = config.guild_of(side)
        channel = client.get_channel(int(channel_id))
        check(channel is not None, f"告知チャンネル {channel_id}（{side}）が bot の控えにある")
        if channel is None:
            continue
        guild = channel.guild
        check(str(guild.id) == guild_id, f"告知チャンネル {channel.name} が {side} のサーバーにある")
        allow = channel.permissions_for(guild.me).value
        for perm in ANNOUNCE_PERMS:
            check(bool(allow & PERMS[perm]), f"  告知チャンネル {channel.name} の bot の権限: {perm}")
        rows = await client.neplector.table("channels", channel_id=channel_id)
        if rows and rows[0].get("reading") == 1:
            print(f"情報  Neplector が告知チャンネル {channel.name} を読んでいます。ここから相手に出せます")
        else:
            print(f"情報  Neplector は告知チャンネル {channel.name} を読んでいません。ここから相手には出せません（相手からは届きます）")
        for name in config.announce_ping_roles:
            found = [role for role in guild.roles if role.name == name]
            check(len(found) == 1, f"  ロール「{name}」が {side} のサーバーに1つある")
            if len(found) == 1 and not found[0].mentionable and not allow & MENTION_EVERYONE:
                print(f"注意  ロール「{name}」は誰でもメンションできる設定ではなく、bot にも「@everyone、@here、全てのロールにメンション」がありません。"
                      "Webhook から通知が鳴らない可能性があります")


async def plan(config: Any, client: Any) -> int:
    actions = await client.engine.sync_structure(dry_run=True)
    print("\n".join(actions) or "することはありません")
    return 0


async def approve_deletes(config: Any, client: Any) -> int:
    print(f"{await client.engine.approve_held_deletes()} 件を消しました")
    return 0


def run() -> int:
    from . import bot

    config, secrets = load_config(), load_secrets()
    try:
        lock = hold_lock(ROOT / "data" / "nepsync.lock")
    except AlreadyRunning:
        print("もう動いています（同じ data のフォルダで2つは動かせません）", file=sys.stderr)
        return 1
    store = Store(ROOT / "data" / "nepsync.db")
    notify = bot.Notifier(secrets.ops_webhook_url, _stamped)
    _stamped(f"動かします。読む位置: {store.get('changes_after') or '（初めて。今から）'}")
    try:
        bot.run_forever(secrets.discord_bot_token, lambda: build(config, secrets, store, notify), log=_stamped)
    except bot.FatalGatewayError as error:
        _stamped(f"Discord につなげません（{error}）。bot の token と設定を確かめてください")
        return 1
    finally:
        lock.close()
    return 0


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows のコンソールでチャンネル名の絵文字で落ちないように
    command = argv[0] if argv else "doctor"
    jobs = {"doctor": doctor, "plan": plan, "approve-deletes": approve_deletes}
    try:
        if command == "run":
            return run()
        if command in jobs:
            return _once(jobs[command])
        print(__doc__)
        return 2
    except ConfigError as error:
        print(f"設定の問題: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
