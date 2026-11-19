"""Neplector の API（aiohttp。discord.py と同じ流れの中で待つ）。本文や秘密の値をログに書かない。

Discord とのやりとりは discord.py に任せていて、ここには無い（ADR 0010）。"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import aiohttp

USER_AGENT = "NepSync (https://example.invalid, 0.2)"
# 新着を待つ問い合わせ（wait つき）に使う接続は、この長さより古くなったら作り直す。
# 公開の住所（Cloudflare 経由）では、待つ問い合わせを繰り返している接続が、できてから30分で黙って答えなくなる
# （2026-11-16 に確かめた。切れた知らせは来ない。短い問い合わせだけの接続では起きない）。その前に替える。
WAIT_CONNECTION_SECONDS = 600
WAIT_SLACK_SECONDS = 15        # 待つ長さに足す、答えを待つ余裕。接続が黙って死んだときに、あきらめるまでの長さになる


class Transient(Exception):
    """しばらくすれば直る失敗。読む位置を進めず、やり直す。"""


def why(error: BaseException) -> str:
    """失敗の理由を、ログに書ける短い文にする。Transient は自分で作った文（場所と種類）を持つ。
    ほかの例外の文は使わない（住所や値が入りうる）。種類の名前と、OS が返した番号だけにする。"""
    if isinstance(error, Transient):
        return f"Transient（{error}）" if str(error) else "Transient"
    number = getattr(error, "errno", None)
    return type(error).__name__ + (f" errno={number}" if isinstance(number, int) else "")


class NeplectorError(Exception):
    """やり直しても直らない答え。discord.HTTPException と同じく status と code を持つ（code は常に None）。"""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"{status} {message}")
        self.status, self.code = status, None


def _parse(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return {"message": body[:120].decode("utf-8", "replace")}


class Neplector:
    def __init__(self, base_url: str, key: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._base, self._key, self._clock = base_url, key, clock
        self._session: aiohttp.ClientSession | None = None
        self._waiter: aiohttp.ClientSession | None = None   # 新着を待つ問い合わせ専用（上の WAIT_CONNECTION_SECONDS）
        self._waiter_born = 0.0

    def _open(self) -> aiohttp.ClientSession:
        """aiohttp のつなぎは、動いている流れの中で作る決まりなので、初めて使うときに作る。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self._session

    async def _open_waiter(self) -> aiohttp.ClientSession:
        """新着を待つ問い合わせ専用のつなぎ。古くなったら、問い合わせの合間に閉じて作り直す。
        待つ問い合わせは一度に1つ（同期の本体の流れ）だけが送るので、閉じるときに使っている最中のものは無い。"""
        now = self._clock()
        if self._waiter is not None and (self._waiter.closed or now - self._waiter_born >= WAIT_CONNECTION_SECONDS):
            await self._waiter.close()
            self._waiter = None
        if self._waiter is None:
            self._waiter, self._waiter_born = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}), now
        return self._waiter

    async def close(self) -> None:
        for session in (self._session, self._waiter):
            if session is not None:
                await session.close()

    async def _fetch(self, url: str, headers: dict[str, str], timeout: float, where: str, waiting: bool = False) -> tuple[int, dict, bytes]:
        """where は、ログに書く「どの問い合わせか」（道だけ。? より後ろや住所は入れない）。"""
        try:
            session = await self._open_waiter() if waiting else self._open()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                return response.status, dict(response.headers), await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise Transient(f"{where} {why(error)}") from error

    async def _get(self, path: str, timeout: float = 30, waiting: bool = False) -> Any:
        where = path.split("?")[0]
        status, headers, body = await self._fetch(self._base + path, {"Authorization": f"Bearer {self._key}"}, timeout, where, waiting)
        parsed = _parse(body)
        if status == 200:
            return parsed
        if status in (429, 503) or status >= 500:
            await asyncio.sleep(float(headers.get("Retry-After") or 5))
            raise Transient(f"{where} {status}")
        raise NeplectorError(status, (parsed or {}).get("error", "") if isinstance(parsed, dict) else "")

    async def latest_seq(self) -> int:
        return int((await self._get("/v1/changes?limit=1")).get("last_seq") or 0)

    async def changes(self, after: int, limit: int = 500, wait: int = 25) -> dict:
        """wait が 1 以上なら、新着が無いとき wait 秒まで待ってから返る。一度に1つだけ呼ぶ（_open_waiter）。"""
        if wait <= 0:
            return await self._get(f"/v1/changes?after={after}&limit={limit}&wait=0")
        return await self._get(f"/v1/changes?after={after}&limit={limit}&wait={wait}", timeout=wait + WAIT_SLACK_SECONDS, waiting=True)

    async def messages(self, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for start in range(0, len(ids), 100):
            body = await self._get("/v1/messages?ids=" + ",".join(ids[start:start + 100]))
            if body.get("bodies_open") is False:
                raise NeplectorError(403, "本文がまだ開いていません")
            found.update({item["message_id"]: item for item in body["messages"]})
        return found

    async def forget(self, after: int, limit: int = 500) -> dict:
        return await self._get(f"/v1/forget?after={after}&limit={limit}")

    async def table(self, name: str, /, **filters: str) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            query = urllib.parse.urlencode({**filters, "limit": 1000, "offset": offset})
            body = await self._get(f"/v1/tables/{name}?{query}")
            rows.extend(body["rows"])
            if not body.get("next_offset"):
                return rows
            offset = body["next_offset"]

    async def download(self, url: str) -> bytes:
        """添付やアイコンの中身を取る（Discord の CDN）。Neplector の鍵は送らない。ファイルには残さない。"""
        status, _, body = await self._fetch(url, {}, 120, "download")
        if status != 200:
            raise NeplectorError(status, "download failed")
        return body
