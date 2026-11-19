"""手元に残すもの（SQLite）。ID の対応、構造の控え、読んだ位置だけ。本文は入れない。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS category_pairs (a_id TEXT PRIMARY KEY, b_id TEXT NOT NULL UNIQUE, snapshot TEXT);
CREATE TABLE IF NOT EXISTS channel_pairs (a_id TEXT PRIMARY KEY, b_id TEXT NOT NULL UNIQUE, category_a TEXT NOT NULL, snapshot TEXT);
CREATE TABLE IF NOT EXISTS webhooks (channel_id TEXT PRIMARY KEY, webhook_id TEXT NOT NULL, token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS thread_pairs (a_id TEXT PRIMARY KEY, b_id TEXT NOT NULL UNIQUE, snapshot TEXT);
CREATE TABLE IF NOT EXISTS message_pairs (
    source_id TEXT PRIMARY KEY, copy_id TEXT NOT NULL UNIQUE, author_id TEXT NOT NULL,
    target_channel TEXT NOT NULL, target_thread TEXT, source_thread TEXT, webhook_id TEXT NOT NULL, edited_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS message_pairs_author ON message_pairs (author_id);
CREATE INDEX IF NOT EXISTS message_pairs_channel ON message_pairs (target_channel);
CREATE TABLE IF NOT EXISTS delete_log (at REAL NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS held_deletes (source_id TEXT PRIMARY KEY, held_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bot_reactions (message_id TEXT NOT NULL, emoji TEXT NOT NULL, PRIMARY KEY (message_id, emoji));
CREATE TABLE IF NOT EXISTS poll_tallies (
    source_id TEXT PRIMARY KEY, expires_at TEXT, source_tally TEXT NOT NULL, source_webhook TEXT NOT NULL, source_thread TEXT,
    copy_tally TEXT NOT NULL, copy_webhook TEXT NOT NULL, copy_thread TEXT, counts TEXT, dirty INTEGER NOT NULL DEFAULT 0,
    sent_at REAL NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS avatar_emojis (
    guild_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, emoji_id TEXT NOT NULL, digest TEXT NOT NULL,
    last_used REAL NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id));
CREATE TABLE IF NOT EXISTS roster_hidden (user_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS roster_stars (user_id TEXT NOT NULL, target_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (user_id, target_id));
CREATE TABLE IF NOT EXISTS roster_picks (user_id TEXT NOT NULL, target_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (user_id, target_id));
CREATE TABLE IF NOT EXISTS reactor_lines (source_id TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS announce_requests (
    message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, action TEXT NOT NULL, requested_at REAL NOT NULL,
    result TEXT, link TEXT, warning TEXT);
CREATE TRIGGER IF NOT EXISTS reactor_lines_cleanup AFTER DELETE ON message_pairs BEGIN
    DELETE FROM reactor_lines WHERE source_id = OLD.source_id;
END;
CREATE TRIGGER IF NOT EXISTS message_pairs_cleanup AFTER DELETE ON message_pairs BEGIN
    DELETE FROM bot_reactions WHERE message_id IN (OLD.source_id, OLD.copy_id);
    DELETE FROM poll_tallies WHERE source_id = OLD.source_id;
END;
"""


def other(side: str) -> str:
    return "b" if side == "a" else "a"


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """古いファイルに、あとから足した列を足す。"""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(avatar_emojis)")}
        if "last_used" not in columns:
            self.db.execute("ALTER TABLE avatar_emojis ADD COLUMN last_used REAL NOT NULL DEFAULT 0")
            self.db.execute("UPDATE avatar_emojis SET last_used = strftime('%s','now')")   # 今あるものは、今から数える
        if "warning" not in {row[1] for row in self.db.execute("PRAGMA table_info(announce_requests)")}:
            self.db.execute("ALTER TABLE announce_requests ADD COLUMN warning TEXT")
        if "pinned" not in {row[1] for row in self.db.execute("PRAGMA table_info(message_pairs)")}:
            # 今ある組は「ピン留めなし」を控えにする。入れる前のピン留めは揃えない（ADR 0012 判断2）
            self.db.execute("ALTER TABLE message_pairs ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

    def _run(self, sql: str, *args: Any) -> sqlite3.Cursor:
        cursor = self.db.execute(sql, args)
        self.db.commit()
        return cursor

    def _rows(self, sql: str, *args: Any) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args)]

    def _one(self, sql: str, *args: Any) -> dict | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", key)
        return row["value"] if row else None

    def set(self, key: str, value: Any) -> None:
        self._run("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", key, str(value))

    def unset(self, key: str) -> None:
        self._run("DELETE FROM kv WHERE key = ?", key)

    # カテゴリ
    def category_pairs(self) -> list[dict]:
        return self._rows("SELECT * FROM category_pairs ORDER BY a_id")

    def save_category_pair(self, a_id: str, b_id: str, snapshot: dict | None) -> None:
        self._run("INSERT OR REPLACE INTO category_pairs VALUES (?, ?, ?)", a_id, b_id, _dump(snapshot))

    # チャンネル
    def channel_pairs(self, category_a: str | None = None) -> list[dict]:
        if category_a is None:
            return self._rows("SELECT * FROM channel_pairs ORDER BY a_id")
        return self._rows("SELECT * FROM channel_pairs WHERE category_a = ? ORDER BY a_id", category_a)

    def save_channel_pair(self, a_id: str, b_id: str, category_a: str, snapshot: dict | None) -> None:
        self._run("INSERT OR REPLACE INTO channel_pairs VALUES (?, ?, ?, ?)", a_id, b_id, category_a, _dump(snapshot))

    def channel_side(self, channel_id: str | None) -> tuple[str, str] | None:
        """(その ID の側, 相手の ID)。組が無ければ None。"""
        if not channel_id:
            return None
        row = self._one("SELECT a_id, b_id FROM channel_pairs WHERE a_id = ? OR b_id = ?", channel_id, channel_id)
        if row is None:
            return None
        return ("a", row["b_id"]) if row["a_id"] == channel_id else ("b", row["a_id"])

    def drop_channel_pair(self, channel_id: str) -> None:
        """組と、それに結びついた対応を消す。"""
        pair = self._one("SELECT a_id, b_id FROM channel_pairs WHERE a_id = ? OR b_id = ?", channel_id, channel_id)
        if pair is None:
            return
        for item in (pair["a_id"], pair["b_id"]):
            self.db.execute("DELETE FROM message_pairs WHERE target_channel = ?", (item,))
            self.db.execute("DELETE FROM webhooks WHERE channel_id = ?", (item,))
        self._run("DELETE FROM channel_pairs WHERE a_id = ?", pair["a_id"])

    # Webhook
    def webhook(self, channel_id: str) -> dict | None:
        return self._one("SELECT * FROM webhooks WHERE channel_id = ?", channel_id)

    def webhook_by_id(self, webhook_id: str) -> dict | None:
        return self._one("SELECT * FROM webhooks WHERE webhook_id = ?", webhook_id)

    def save_webhook(self, channel_id: str, webhook_id: str, token: str) -> None:
        self._run("INSERT OR REPLACE INTO webhooks VALUES (?, ?, ?)", channel_id, webhook_id, token)

    def drop_webhook(self, channel_id: str) -> None:
        self._run("DELETE FROM webhooks WHERE channel_id = ?", channel_id)

    # スレッド
    def thread_side(self, thread_id: str | None) -> tuple[str, str, dict | None] | None:
        if not thread_id:
            return None
        row = self._one("SELECT * FROM thread_pairs WHERE a_id = ? OR b_id = ?", thread_id, thread_id)
        if row is None:
            return None
        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
        return ("a", row["b_id"], snapshot) if row["a_id"] == thread_id else ("b", row["a_id"], snapshot)

    def save_thread_pair(self, side: str, source_id: str, copy_id: str, snapshot: dict | None) -> None:
        a_id, b_id = (source_id, copy_id) if side == "a" else (copy_id, source_id)
        self._run("INSERT OR REPLACE INTO thread_pairs VALUES (?, ?, ?)", a_id, b_id, _dump(snapshot))

    def drop_thread_pair(self, thread_id: str) -> None:
        pair = self._one("SELECT a_id, b_id FROM thread_pairs WHERE a_id = ? OR b_id = ?", thread_id, thread_id)
        if pair is None:
            return
        self.db.execute("DELETE FROM message_pairs WHERE target_thread IN (?, ?)", (pair["a_id"], pair["b_id"]))
        self._run("DELETE FROM thread_pairs WHERE a_id = ?", pair["a_id"])

    # 発言
    def save_message_pair(self, source_id: str, copy_id: str, author_id: str, target_channel: str, target_thread: str | None,
                          source_thread: str | None, webhook_id: str, edited_at: str | None) -> None:
        self._run("INSERT OR REPLACE INTO message_pairs (source_id, copy_id, author_id, target_channel, target_thread, source_thread, webhook_id, edited_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", source_id, copy_id, author_id, target_channel, target_thread, source_thread, webhook_id, edited_at)

    def set_message_pinned(self, source_id: str, pinned: bool) -> None:
        """ピン留めの控え（ADR 0012）。"""
        self._run("UPDATE message_pairs SET pinned = ? WHERE source_id = ?", int(pinned), source_id)

    def message_by_source(self, source_id: str) -> dict | None:
        return self._one("SELECT * FROM message_pairs WHERE source_id = ?", source_id)

    def message_by_copy(self, copy_id: str) -> dict | None:
        return self._one("SELECT * FROM message_pairs WHERE copy_id = ?", copy_id)

    # アイコンの絵文字（ADR 0004 判断8）。絵文字の ID と、アイコンの URL のハッシュだけ
    def avatar_emoji(self, guild_id: str, user_id: str) -> dict | None:
        return self._one("SELECT * FROM avatar_emojis WHERE guild_id = ? AND user_id = ?", guild_id, user_id)

    def avatar_emojis_of(self, user_id: str) -> list[dict]:
        return self._rows("SELECT * FROM avatar_emojis WHERE user_id = ?", user_id)

    def all_avatar_emojis(self) -> list[dict]:
        return self._rows("SELECT * FROM avatar_emojis")

    def save_avatar_emoji(self, guild_id: str, user_id: str, name: str, emoji_id: str, digest: str, last_used: float) -> None:
        self._run("INSERT OR REPLACE INTO avatar_emojis (guild_id, user_id, name, emoji_id, digest, last_used) VALUES (?, ?, ?, ?, ?, ?)",
                  guild_id, user_id, name, emoji_id, digest, last_used)

    def touch_avatar_emoji(self, guild_id: str, user_id: str, last_used: float) -> None:
        self._run("UPDATE avatar_emojis SET last_used = ? WHERE guild_id = ? AND user_id = ?", last_used, guild_id, user_id)

    def drop_avatar_emoji(self, guild_id: str, user_id: str) -> None:
        self._run("DELETE FROM avatar_emojis WHERE guild_id = ? AND user_id = ?", guild_id, user_id)

    # 名簿（/nepsync-members）に載せない人。ID だけ
    def hidden_users(self) -> set[str]:
        return {row["user_id"] for row in self._rows("SELECT user_id FROM roster_hidden")}

    def set_hidden(self, user_id: str, hidden: bool) -> None:
        if hidden:
            self._run("INSERT OR REPLACE INTO roster_hidden VALUES (?, strftime('%s','now'))", user_id)
        else:
            self._run("DELETE FROM roster_hidden WHERE user_id = ?", user_id)

    # 名簿の並び（ADR 0014 判断3）。ID の組と時刻だけ
    def stars(self, user_id: str) -> list[str]:
        return [row["target_id"] for row in self._rows("SELECT target_id FROM roster_stars WHERE user_id = ? ORDER BY at DESC", user_id)]

    def set_star(self, user_id: str, target_id: str, on: bool, at: float) -> None:
        if on:
            self._run("INSERT OR REPLACE INTO roster_stars VALUES (?, ?, ?)", user_id, target_id, at)
        else:
            self._run("DELETE FROM roster_stars WHERE user_id = ? AND target_id = ?", user_id, target_id)

    def picks(self, user_id: str) -> list[str]:
        return [row["target_id"] for row in self._rows("SELECT target_id FROM roster_picks WHERE user_id = ? ORDER BY at DESC", user_id)]

    def add_picks(self, user_id: str, target_ids: list[str], at: float, keep: int) -> None:
        """選んだ相手を足し、新しいほうから keep 人だけ残す。"""
        for target_id in target_ids:
            self.db.execute("INSERT OR REPLACE INTO roster_picks VALUES (?, ?, ?)", (user_id, target_id, at))
        self.db.execute("DELETE FROM roster_picks WHERE user_id = ? AND target_id NOT IN "
                        "(SELECT target_id FROM roster_picks WHERE user_id = ? ORDER BY at DESC LIMIT ?)", (user_id, user_id, keep))
        self.db.commit()

    def forget_roster_user(self, user_id: str) -> None:
        """Neplector から外れた人の分を、選んだ側でも選ばれた側でも消す。"""
        for table in ("roster_stars", "roster_picks"):
            self.db.execute(f"DELETE FROM {table} WHERE user_id = ? OR target_id = ?", (user_id, user_id))
        self.db.commit()

    def recent_authors(self, target_channel: str, limit: int) -> list[str]:
        """このチャンネルに写しが届いた人を、新しい順に。発言の ID（作られた時刻が入っている）で並べる。"""
        rows = self._rows("SELECT author_id, MAX(CAST(source_id AS INTEGER)) AS last FROM message_pairs WHERE target_channel = ? "
                          "GROUP BY author_id ORDER BY last DESC LIMIT ?", target_channel, limit)
        return [row["author_id"] for row in rows]

    def messages_by_author(self, author_id: str) -> list[dict]:
        return self._rows("SELECT * FROM message_pairs WHERE author_id = ?", author_id)

    def set_message_edited(self, source_id: str, edited_at: str | None) -> None:
        self._run("UPDATE message_pairs SET edited_at = ? WHERE source_id = ?", edited_at, source_id)

    def drop_message(self, source_id: str) -> None:
        self._run("DELETE FROM message_pairs WHERE source_id = ?", source_id)

    # bot が付けたリアクション（元に戻すときに、自分の分だけを外すため）
    def bot_reactions(self, message_id: str) -> list[str]:
        return [row["emoji"] for row in self._rows("SELECT emoji FROM bot_reactions WHERE message_id = ?", message_id)]

    def add_bot_reaction(self, message_id: str, emoji: str) -> None:
        self._run("INSERT OR IGNORE INTO bot_reactions VALUES (?, ?)", message_id, emoji)

    def drop_bot_reaction(self, message_id: str, emoji: str) -> None:
        self._run("DELETE FROM bot_reactions WHERE message_id = ? AND emoji = ?", message_id, emoji)

    def clear_bot_reactions(self, message_id: str, emoji: str | None) -> None:
        if emoji:
            self.drop_bot_reaction(message_id, emoji)
        else:
            self._run("DELETE FROM bot_reactions WHERE message_id = ?", message_id)

    # やめた機能（写しの末尾の名前）で、名前を書いた写し。後片付けのあいだだけ使う（ADR 0004 判断7）
    def reactor_line_sources(self) -> list[str]:
        return [row["source_id"] for row in self._rows("SELECT source_id FROM reactor_lines")]

    def drop_reactor_line(self, source_id: str) -> None:
        self._run("DELETE FROM reactor_lines WHERE source_id = ?", source_id)

    # 投票の合計の発言。票の数だけを控え、題名や選択肢の文は持たない
    def save_tally(self, source_id: str, expires_at: str | None, source_tally: str, source_webhook: str, source_thread: str | None,
                   copy_tally: str, copy_webhook: str, copy_thread: str | None, sent_at: float) -> None:
        self._run("INSERT OR REPLACE INTO poll_tallies (source_id, expires_at, source_tally, source_webhook, source_thread, copy_tally, copy_webhook, copy_thread, sent_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", source_id, expires_at, source_tally, source_webhook, source_thread, copy_tally, copy_webhook, copy_thread, sent_at)

    def tally(self, source_id: str) -> dict | None:
        return self._one("SELECT * FROM poll_tallies WHERE source_id = ?", source_id)

    def tally_by_message(self, message_id: str) -> dict | None:
        """「投票の合計」の発言（どちらのサーバーのものでも）から、その投票の行。"""
        return self._one("SELECT * FROM poll_tallies WHERE source_tally = ? OR copy_tally = ?", message_id, message_id)

    def open_tallies(self) -> list[dict]:
        return self._rows("SELECT * FROM poll_tallies WHERE closed = 0")

    def mark_tally_dirty(self, source_id: str) -> None:
        self._run("UPDATE poll_tallies SET dirty = 1 WHERE source_id = ?", source_id)

    def update_tally(self, source_id: str, counts: str, sent_at: float, closed: bool) -> None:
        self._run("UPDATE poll_tallies SET counts = ?, sent_at = ?, closed = ?, dirty = 0 WHERE source_id = ?", counts, sent_at, int(closed), source_id)

    def clean_tally(self, source_id: str) -> None:
        self._run("UPDATE poll_tallies SET dirty = 0 WHERE source_id = ?", source_id)

    def close_tally(self, source_id: str) -> None:
        self._run("UPDATE poll_tallies SET closed = 1, dirty = 0 WHERE source_id = ?", source_id)

    def drop_tally(self, source_id: str) -> None:
        self._run("DELETE FROM poll_tallies WHERE source_id = ?", source_id)

    # 告知を出す・下げる依頼（ADR 0009）。bot のスレッドが書き、同期の本体が処理する。ID と結果の記号だけ
    def request_announce(self, message_id: str, channel_id: str, action: str, at: float) -> None:
        self._run("INSERT OR REPLACE INTO announce_requests (message_id, channel_id, action, requested_at) VALUES (?, ?, ?, ?)",
                  message_id, channel_id, action, at)

    def announce_request(self, message_id: str) -> dict | None:
        return self._one("SELECT * FROM announce_requests WHERE message_id = ?", message_id)

    def pending_announces(self) -> list[dict]:
        return self._rows("SELECT * FROM announce_requests WHERE result IS NULL ORDER BY requested_at")

    def finish_announce(self, message_id: str, result: str, link: str | None = None, warning: list | None = None) -> None:
        """warning：相手のサーバーで開けないリンク（ID だけの URL と種類）。"""
        self._run("UPDATE announce_requests SET result = ?, link = ?, warning = ? WHERE message_id = ?",
                  result, link, json.dumps(warning) if warning else None, message_id)

    def purge_announces(self, before: float) -> None:
        self._run("DELETE FROM announce_requests WHERE requested_at < ?", before)

    # 自動で消した記録
    def deletes_since(self, since: float) -> int:
        return self._one("SELECT COUNT(*) AS n FROM delete_log WHERE at >= ?", since)["n"]

    def log_delete(self, at: float, source_id: str, target_id: str) -> None:
        self._run("INSERT INTO delete_log VALUES (?, ?, ?)", at, source_id, target_id)

    def hold_delete(self, source_id: str, at: float) -> None:
        self._run("INSERT OR REPLACE INTO held_deletes VALUES (?, ?)", source_id, at)

    def held_deletes(self) -> list[str]:
        return [row["source_id"] for row in self._rows("SELECT source_id FROM held_deletes ORDER BY held_at")]

    def release_held(self, source_id: str) -> None:
        self._run("DELETE FROM held_deletes WHERE source_id = ?", source_id)


def _dump(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
