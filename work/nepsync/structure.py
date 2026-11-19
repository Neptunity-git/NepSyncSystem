"""構造の見比べ。通信しない。両方の今の姿（discord.py のチャンネル）と控えから、すること（Plan）を決める。

ID は SQLite の控えと同じく文字で扱う（discord.py は数で持つ）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import discord

TEXT, FORUM = discord.ChannelType.text, discord.ChannelType.forum
SYNCED_TYPES = (TEXT, FORUM)
ALL_PERMISSIONS = (1 << 64) - 1


def sid(item: Any) -> str:
    return str(item.id)


def fields_of(channel: Any) -> dict:
    """同期する項目を、比べられる形にする。空の表現の違いをそろえる。
    控えに残す形でもあるので、名前（rate_limit_per_user など）は前の版から変えない。"""
    result = {"name": channel.name or "", "topic": getattr(channel, "topic", None) or "",
              "rate_limit_per_user": getattr(channel, "slowmode_delay", 0) or 0, "nsfw": bool(getattr(channel, "nsfw", False))}
    if channel.type == FORUM:
        result["tags"] = [{"name": tag.name, "moderated": bool(tag.moderated), "emoji_name": _unicode_emoji(tag.emoji)}
                          for tag in channel.available_tags]
    return result


def _unicode_emoji(emoji: Any) -> str | None:
    """タグの絵文字は、ふつうの絵文字だけ写す（カスタム絵文字の ID はサーバーごとに違う）。"""
    if emoji is None or getattr(emoji, "id", None):
        return None
    return emoji.name or None


@dataclass
class Merge:
    write_a: dict = field(default_factory=dict)
    write_b: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def merge(a: dict, b: dict, snapshot: dict | None, *, flow_a: bool, flow_b: bool, priority: str) -> Merge:
    """項目ごとに、どちらの値にそろえるかを決める。流してよくない側の値では、相手を書き換えない。"""
    result = Merge()
    for key in a.keys() | b.keys():
        value_a, value_b = a.get(key), b.get(key)
        if value_a == value_b:
            result.snapshot[key] = value_a
            continue
        if flow_a and not flow_b:
            winner = "a"
        elif flow_b and not flow_a:
            winner = "b"
        elif snapshot is None:
            winner = priority
        else:
            changed_a, changed_b = value_a != snapshot.get(key), value_b != snapshot.get(key)
            if changed_a and changed_b:
                winner = priority
                result.conflicts.append(key)
            else:
                winner = "a" if changed_a else "b"
        if winner == "a":
            result.write_b[key] = value_a
            result.snapshot[key] = value_a
        else:
            result.write_a[key] = value_b
            result.snapshot[key] = value_b
    return result


@dataclass
class Plan:
    new_pairs: list[tuple[str, str]] = field(default_factory=list)       # (a の ID, b の ID)
    creates: list[tuple[str, Any]] = field(default_factory=list)         # (作る側, 元のチャンネル)
    updates: list[tuple[str, str, Merge]] = field(default_factory=list)  # (a の ID, b の ID, 差)
    missing: list[tuple[str, str]] = field(default_factory=list)         # (無い側, 無い ID)。ここでは消さない
    notices: list[str] = field(default_factory=list)


def plan(children_a: list, children_b: list, pairs: list[dict], readable: set[str], *,
         flow_a: bool, flow_b: bool, priority: str) -> Plan:
    """カテゴリの中を見比べる。

    新しく写してよいのは、Neplector が読んでいるチャンネル（readable）だけ。
    一覧に無いチャンネルは missing に入れるだけで、消えたとは判断しない。"""
    result = Plan()
    by_a = {sid(item): item for item in children_a}
    by_b = {sid(item): item for item in children_b}
    paired_a, paired_b = {pair["a_id"] for pair in pairs}, {pair["b_id"] for pair in pairs}
    for pair in pairs:
        left, right = by_a.get(pair["a_id"]), by_b.get(pair["b_id"])
        if left is None:
            result.missing.append(("a", pair["a_id"]))
        if right is None:
            result.missing.append(("b", pair["b_id"]))
        if left is None or right is None:
            continue
        snapshot = _loads(pair.get("snapshot"))
        change = merge(fields_of(left), fields_of(right), snapshot, flow_a=flow_a, flow_b=flow_b, priority=priority)
        if change.write_a or change.write_b or change.snapshot != snapshot:
            result.updates.append((pair["a_id"], pair["b_id"], change))

    for items in (children_a, children_b):
        for item in items:
            if item.type not in SYNCED_TYPES and sid(item) in readable:
                result.notices.append(f"チャンネル {sid(item)} は種類が対象外なので同期しません（type={item.type.value}）")

    free_a = [item for item in children_a if sid(item) not in paired_a and item.type in SYNCED_TYPES]
    free_b = [item for item in children_b if sid(item) not in paired_b and item.type in SYNCED_TYPES]
    taken_a, taken_b = set(), set()
    for key in sorted({(item.type.value, item.name) for item in free_a + free_b}):
        left = [item for item in free_a if (item.type.value, item.name) == key]
        right = [item for item in free_b if (item.type.value, item.name) == key]
        if not left or not right:
            continue
        if len(left) == 1 and len(right) == 1:
            result.new_pairs.append((sid(left[0]), sid(right[0])))
        else:
            result.notices.append(f"同じ種類で同じ名前のチャンネルが複数あるので、組にしません（{key[1]}）")
        taken_a.update(sid(item) for item in left)
        taken_b.update(sid(item) for item in right)

    if flow_a:
        result.creates.extend(("b", item) for item in free_a if sid(item) not in taken_a and sid(item) in readable)
    if flow_b:
        result.creates.extend(("a", item) for item in free_b if sid(item) not in taken_b and sid(item) in readable)
    return result


def effective_permissions(guild_id: str, user_id: str, user_roles: Iterable[str], roles: Iterable[Any],
                          overwrites: Mapping[Any, discord.PermissionOverwrite]) -> int:
    """Discord の決まりどおりに、その人の実際の権限を計算する。全員 → ロール → チャンネルの上書き（全員 → ロール → 本人）の順。

    名簿の人は Neplector から来る（discord.py の Member は無い）ので、discord.py の permissions_for は使えない。
    roles は discord.py の Role、overwrites は channel.overwrites（相手 → PermissionOverwrite）。"""
    user_roles = set(user_roles)
    by_id = {sid(role): role.permissions.value for role in roles}
    allowed = by_id.get(guild_id, 0)
    for role_id in user_roles:
        allowed |= by_id.get(role_id, 0)
    if allowed & discord.Permissions(administrator=True).value:
        return ALL_PERMISSIONS
    by_target = {sid(target): [side.value for side in overwrite.pair()] for target, overwrite in overwrites.items()}
    if guild_id in by_target:
        allow, deny = by_target[guild_id]
        allowed = (allowed & ~deny) | allow
    deny = allow = 0
    for role_id in user_roles:
        if role_id in by_target:
            allow |= by_target[role_id][0]
            deny |= by_target[role_id][1]
    allowed = (allowed & ~deny) | allow
    if user_id in by_target:
        allow, deny = by_target[user_id]
        allowed = (allowed & ~deny) | allow
    return allowed


def order_of(children: list) -> list[str]:
    return [sid(item) for item in sorted(children, key=lambda item: (item.position, item.id))]


def reorder_plan(children_a: list, children_b: list, pairs: list[dict], last_order: list[str] | None, *,
                 flow_a: bool, flow_b: bool, priority: str) -> tuple[str | None, list[str], list[str]]:
    """組になったチャンネルの並び順を比べる。(書く側, その側の ID の目標の並び, 新しい控え) を返す。控えは a の ID の並び。"""
    a_to_b = {pair["a_id"]: pair["b_id"] for pair in pairs}
    b_to_a = {value: key for key, value in a_to_b.items()}
    now_a = [item for item in order_of(children_a) if item in a_to_b]
    now_b_as_a = [b_to_a[item] for item in order_of(children_b) if item in b_to_a]
    if set(now_a) != set(now_b_as_a):   # 片方が見えていないときは触らない
        return None, [], last_order or now_a
    if now_a == now_b_as_a:
        return None, [], now_a
    if flow_a and not flow_b:
        winner = "a"
    elif flow_b and not flow_a:
        winner = "b"
    elif last_order is None or (now_a != last_order and now_b_as_a != last_order):
        winner = priority
    else:
        winner = "a" if now_a != last_order else "b"
    if winner == "a":
        return "b", [a_to_b[item] for item in now_a], now_a
    return "a", now_b_as_a, now_b_as_a


def next_move(current: list[str], goal: list[str]) -> tuple[str, str] | None:
    """目標の並びに近づける次の1手。(動かすチャンネル, その前に置くチャンネル)。そろっていれば None。
    current は今の並び（目標に出てくるものだけ）。"""
    for index, wanted in enumerate(goal):
        if index >= len(current):
            return None
        if current[index] != wanted:
            return wanted, current[index]
    return None


def _loads(text: str | None) -> dict | None:
    return json.loads(text) if text else None
