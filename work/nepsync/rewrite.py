"""本文の書き換え。通信しない（名前や対応は、呼び出し側が関数で渡す）。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import unquote, urlsplit

MAX_CONTENT = 2000
MAX_USERNAME = 80
USER_MENTION = re.compile(r"<@!?(\d+)>")
ROLE_MENTION = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION = re.compile(r"<#(\d+)>")
MESSAGE_LINK = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?")
FORBIDDEN_IN_NAME = re.compile(r"discord|clyde", re.IGNORECASE)
UNSAFE_IN_FILENAME = re.compile(r'[\x00-\x1f"\\/]')
MAX_FILENAME = 200
TRUNCATED = "…（長いので省略しました）"


def rewrite_body(text: str, *, user_name: Callable[[str], str | None], role_name: Callable[[str], str | None],
                 channel_map: Callable[[str], str | None], channel_name: Callable[[str], str | None],
                 message_map: Callable[[str], str | None], target_guild: str, keep_user: Callable[[str], bool] | None = None,
                 role_map: Callable[[str], str | None] | None = None) -> str:
    """呼び出しを名前の文字に、組になったチャンネルと発言へのリンクを相手側に書き換える。それ以外のリンクはそのまま。
    keep_user：本物のメンションのまま残す人を決める関数（相手のサーバーにいる人）。
    role_map：相手のサーバーの同じロールの ID を返す関数。返したロールは本物のメンションにする（告知の member。ADR 0009）。"""
    def role_mention(match: re.Match) -> str:
        mapped = role_map(match.group(1)) if role_map is not None else None
        if mapped:
            return f"<@&{mapped}>"
        return f"@{role_name(match.group(1)) or '不明なロール'}"

    def channel_mention(match: re.Match) -> str:
        mapped = channel_map(match.group(1))
        if mapped:
            return f"<#{mapped}>"
        name = channel_name(match.group(1))
        return f"#{name}" if name else match.group(0)

    def link(match: re.Match) -> str:
        mapped = channel_map(match.group(2))
        if not mapped:
            return match.group(0)
        message = match.group(3)
        mapped_message = message_map(message) if message else None
        tail = f"/{mapped_message}" if mapped_message else ""
        return f"https://discord.com/channels/{target_guild}/{mapped}{tail}"

    def user_mention(match: re.Match) -> str:
        if keep_user is not None and keep_user(match.group(1)):
            return f"<@{match.group(1)}>"
        return f"@{user_name(match.group(1)) or '不明なメンバー'}"

    text = USER_MENTION.sub(user_mention, text)
    text = ROLE_MENTION.sub(role_mention, text)
    text = MESSAGE_LINK.sub(link, text)
    return CHANNEL_MENTION.sub(channel_mention, text)


def dead_links(text: str, source_guild: str, channel_map: Callable[[str], str | None],
               message_map: Callable[[str], str | None]) -> list[tuple[str, str]]:
    """相手のサーバーで、指している発言を開けないリンク（ADR 0009 判断5）。(リンク, 種類) の並び。
    種類は message（発言が同期されていないので、相手側ではチャンネルへのリンクになる）か、
    channel（チャンネルが組になっていないので、元のサーバーのリンクのまま）。ほかのサーバーへのリンクは見ない。"""
    found = []
    for match in MESSAGE_LINK.finditer(text):
        if match.group(1) != source_guild:
            continue
        if not channel_map(match.group(2)):
            found.append((match.group(0), "channel"))
        elif match.group(3) and not message_map(match.group(3)):
            found.append((match.group(0), "message"))
    return found


def quote_line(name: str | None, text: str | None, link: str | None) -> str:
    """返信の引用を1行で。"""
    if name is None:
        return "> ↩️ （返信先は表示できません）"
    snippet = " ".join((text or "").split())
    if len(snippet) > 60:
        snippet = snippet[:60] + "…"
    tail = f" [発言へ]({link})" if link else ""
    return f"> ↩️ **{name}**: {snippet}{tail}".rstrip()


def delay_mark(created_at: str, now: datetime, minutes: int) -> str:
    """一定時間より遅れた発言に、元の時刻の印を付ける。時刻は見る人の時間帯で表示される。"""
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if (now - created).total_seconds() <= minutes * 60:
        return ""
    return f"-# 🕒 <t:{int(created.timestamp())}:f> の発言（遅れて同期）"


def snowflake_time(snowflake: str) -> datetime:
    """Discord の ID に入っている、作られた時刻。"""
    return datetime.fromtimestamp(((int(snowflake) >> 22) + 1420070400000) / 1000, timezone.utc)


def compose(*, mark: str = "", quote: str = "", forward: str | None = None, body: str = "", notes: list[str] | None = None) -> str:
    """1つの投稿にまとめる。2000文字を超えるときは本文を削る。"""
    head = [item for item in (mark, quote) if item]
    if forward:
        head.extend(f"> {line}" for line in forward.splitlines())
    tail = list(notes or [])
    used = sum(len(item) + 1 for item in head + tail)
    room = MAX_CONTENT - used
    if len(body) > room:
        body = body[:max(room - len(TRUNCATED), 0)] + TRUNCATED
    text = "\n".join(item for item in [*head, body, *tail] if item)
    return text[:MAX_CONTENT]


def webhook_username(name: str | None) -> str:
    """Webhook の名前の決まりに合わせる（discord と clyde は使えない）。"""
    cleaned = FORBIDDEN_IN_NAME.sub(lambda m: m.group(0)[0] + "​" + m.group(0)[1:], (name or "").strip())
    return cleaned[:MAX_USERNAME] or "メンバー"


def filename_from_url(url: str | None, extension: str | None) -> str | None:
    """添付の URL に入っているファイル名を取り出す。拡張子が記録と合わないときは信用しない。"""
    if not url:
        return None
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = UNSAFE_IN_FILENAME.sub("_", name).strip()[:MAX_FILENAME]
    suffix = (extension or "").lstrip(".").lower()
    if not name or (suffix and not name.lower().endswith("." + suffix)):
        return None
    return name


def filename_from_title(title: str | None, extension: str | None) -> str | None:
    """Discord の title から名前を作る。title は拡張子を持たず、日本語もそのまま残っている。"""
    name = UNSAFE_IN_FILENAME.sub("_", title or "").strip()
    if not name:
        return None
    suffix = (extension or "").lstrip(".")
    if suffix and not name.lower().endswith("." + suffix.lower()):
        name = f"{name[:MAX_FILENAME - len(suffix) - 1]}.{suffix}"
    return name[:MAX_FILENAME]


def attachment_filename(index: int, extension: str | None, spoiler: bool, url: str | None = None, title: str | None = None,
                        filename: str | None = None) -> str:
    """元のファイル名を引き継ぐ。順に、title（Discord が残す元の名前）、Neplector の filename、URL の名前。
    どれも無い古い添付は、拡張子だけ合わせた名前を作る。隠す画像は SPOILER_ で始める。"""
    suffix = (extension or "").lstrip(".")
    name = (filename_from_title(title, extension) or filename_from_title(filename, extension)
            or filename_from_url(url, extension) or f"file{index}" + (f".{suffix}" if suffix else ""))
    return f"SPOILER_{name}" if spoiler and not name.startswith("SPOILER_") else name
