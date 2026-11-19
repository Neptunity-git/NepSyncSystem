"""設定を読む。秘密の値は環境変数か .env、それ以外は data/nepsync.toml（.env と data はコミットしない）。"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOTH, A_TO_B = "both", "a_to_b"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    guild_a: str
    guild_b: str
    category_names: tuple[str, ...]
    direction: str = A_TO_B
    priority: str = "a"                     # 両方で同じ項目が変わったときに勝つ側
    sync_messages: bool = True
    sync_structure: bool = True
    sync_reactions: bool = True
    avatar_emoji_days: int = 90             # アイコンの絵文字を、この日数表示されなければ消す（ADR 0004 判断9）
    slash_commands: bool = True             # /nepsync-members。これだけ Discord と常時接続する（知らせは受けない）
    sync_polls: bool = True
    sync_pins: bool = True                  # ピン留めの付け外しを写す（ADR 0012）
    allow_delete: bool = True
    delete_limit_per_hour: int = 3
    delay_mark_minutes: int = 10
    structure_interval_seconds: int = 300
    forget_interval_seconds: int = 3600
    max_upload_bytes: int = 20 * 1024 * 1024   # ブーストされても上げない（ADR 0006）
    announcements: tuple[tuple[str, str], ...] = ()   # 告知チャンネルの組 (a, b)。右クリックで選んだものだけ出す（ADR 0009）
    announce_ping_roles: tuple[str, ...] = ()          # 告知で、相手のサーバーでも通知を鳴らすロールの名前

    def guild_of(self, side: str) -> str:
        return self.guild_a if side == "a" else self.guild_b

    def flows_from(self, side: str) -> bool:
        """その側の変更を、相手に流してよいか。"""
        return self.direction == BOTH or side == "a"

    def announce_side(self, channel_id: str | None) -> tuple[str, str] | None:
        """告知チャンネルなら (その側, 相手の告知チャンネル)。"""
        for a_id, b_id in self.announcements:
            if channel_id == a_id:
                return "a", b_id
            if channel_id == b_id:
                return "b", a_id
        return None


@dataclass(frozen=True)
class Secrets:
    neplector_base_url: str
    neplector_api_key: str
    discord_bot_token: str
    ops_webhook_url: str


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "data" / "nepsync.toml"
    if not path.exists():
        raise ConfigError(f"{path} がありません。nepsync.example.toml をコピーして作ってください")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    known = Config.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"知らない設定があります: {sorted(unknown)[0]}")
    for key in ("guild_a", "guild_b", "category_names"):
        if not data.get(key):
            raise ConfigError(f"{key} を書いてください")
    data["category_names"] = tuple(data["category_names"])
    pairs = data.get("announcements", [])
    if not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(item, str) and item.isdigit() for item in pair) for pair in pairs):
        raise ConfigError('announcements は [["a の告知チャンネルの ID", "b の告知チャンネルの ID"]] の形で書いてください')
    data["announcements"] = tuple((pair[0], pair[1]) for pair in pairs)
    data["announce_ping_roles"] = tuple(data.get("announce_ping_roles", ()))
    config = Config(**data)
    if config.direction not in (BOTH, A_TO_B):
        raise ConfigError("direction は both か a_to_b です")
    if config.priority not in ("a", "b"):
        raise ConfigError("priority は a か b です")
    return config


SECRET_KEYS = ("NEPLECTOR_BASE_URL", "NEPLECTOR_API_KEY", "DISCORD_BOT_TOKEN", "OPS_WEBHOOK_URL")


def load_secrets(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Secrets:
    """環境変数に値があればそれを使い、無いものは .env から読む（ADR 0013 判断2。コンテナでは環境変数で渡す）。.env は無くてもよい。"""
    path = path or ROOT / ".env"
    environ = os.environ if environ is None else environ
    values: dict[str, str] = {}
    if path.is_file():
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip('"').strip("'")
    for key in SECRET_KEYS:
        if (environ.get(key) or "").strip():
            values[key] = environ[key].strip()
    missing = [key for key in SECRET_KEYS if not values.get(key)]
    if missing:
        raise ConfigError(f"環境変数にも .env にも値がありません: {missing[0]}")
    return Secrets(values["NEPLECTOR_BASE_URL"].rstrip("/"), values["NEPLECTOR_API_KEY"], values["DISCORD_BOT_TOKEN"], values["OPS_WEBHOOK_URL"])
