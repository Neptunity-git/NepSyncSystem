"""Neplector の API を読む部分のテスト。この機械の中に立てた偽物のサーバーにだけつなぐ。データはすべて架空。"""

import asyncio
import json
import unittest
import unittest.mock

from nepsync.neplector import WAIT_CONNECTION_SECONDS, Neplector, NeplectorError, Transient, why

KEY = "fictional-key"


class FakeServer:
    """筋書きどおりに答える、小さな HTTP サーバー。筋書きは1つの問い合わせにつき1つ：
    (status, 本文, 種類) で答える / "hang" で答えずに黙る / "drop" で答えずに切る。筋書きが尽きたら 200 の空の JSON。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[str] = []       # 受けた問い合わせの道（鍵は入れない）
        self.connections = 0
        self.closed_by_client = 0           # 相手（NepSync）の側から閉じられた接続の数
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *_):
        self._server.close()
        for writer in self._writers:     # 開いたままの接続があると、閉じ終わるのを待ち続ける
            writer.close()
        await self._server.wait_closed()

    async def _serve(self, reader, writer):
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                self.requests.append(head.split(b" ", 2)[1].decode())
                step = self.script.pop(0) if self.script else (200, b"{}", "application/json")
                if step == "hang":
                    if not await reader.read():     # 相手が閉じるまで黙って待つ
                        self.closed_by_client += 1
                    return
                if step == "drop":
                    return
                status, body, kind, *extra = step
                lines = [f"HTTP/1.1 {status} X", f"Content-Type: {kind}", f"Content-Length: {len(body)}", *extra, "", ""]
                writer.write("\r\n".join(lines).encode() + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed_by_client += 1
        finally:
            writer.close()


def page(**fields) -> tuple:
    return 200, json.dumps(fields).encode(), "application/json"


class Client(unittest.IsolatedAsyncioTestCase):
    async def client(self, server: FakeServer) -> Neplector:
        nep = Neplector(server.url, KEY)
        self.addAsyncCleanup(nep.close)
        return nep

    async def test_reads_json_and_reuses_the_connection(self):
        async with FakeServer([page(last_seq=7), page(rows=[], last_seq=7)]) as server:
            nep = await self.client(server)
            self.assertEqual(await nep.latest_seq(), 7)
            self.assertEqual(await nep.changes(7, wait=0), {"rows": [], "last_seq": 7})
            self.assertEqual(server.connections, 1)
            self.assertEqual(server.requests[1], "/v1/changes?after=7&limit=500&wait=0")

    async def test_refusal_is_not_transient(self):
        async with FakeServer([(401, json.dumps({"error": "x"}).encode(), "application/json")]) as server:
            nep = await self.client(server)
            with self.assertRaises(NeplectorError) as caught:
                await nep.latest_seq()
            self.assertEqual(caught.exception.status, 401)

    async def test_error_page_in_front_is_transient_and_says_where(self):
        async with FakeServer([(502, b"<html>bad gateway</html>", "text/html", "Retry-After: 0")]) as server:
            nep = await self.client(server)
            with self.assertRaises(Transient) as caught:
                await nep.changes(3, wait=0)
            self.assertEqual(str(caught.exception), "/v1/changes 502")

    async def test_reason_never_carries_the_key_or_the_query(self):
        async with FakeServer(["drop", "drop"]) as server:
            nep = await self.client(server)
            with self.assertRaises(Transient) as caught:
                await nep.messages(["111", "222"])
            reason = why(caught.exception)
            self.assertIn("/v1/messages", reason)
            self.assertIn("ServerDisconnectedError", reason)
            for secret in (KEY, "111", "127.0.0.1"):
                self.assertNotIn(secret, reason)

    async def test_waiting_has_its_own_connection_and_replaces_it_before_it_gets_old(self):
        """待つ問い合わせの接続は、30分で黙って切られる前に作り直す（古い接続は、こちらから閉じる）。"""
        clock = [1000.0]
        async with FakeServer([]) as server:
            nep = Neplector(server.url, KEY, clock=lambda: clock[0])
            self.addAsyncCleanup(nep.close)
            await nep.changes(1)
            clock[0] += WAIT_CONNECTION_SECONDS - 1
            await nep.changes(1)
            self.assertEqual(server.connections, 1)          # まだ古くないので、同じ接続を使う
            clock[0] += 1
            await nep.changes(1)
            self.assertEqual(server.connections, 2)          # 古くなったので、新しい接続
            await asyncio.sleep(0.05)
            self.assertEqual(server.closed_by_client, 1)     # 古いほうは閉じてある
            await nep.changes(1)
            self.assertEqual(server.connections, 2)          # 作り直したあとは、また使い回す
            await nep.latest_seq()
            self.assertEqual(server.connections, 3)          # 待たない問い合わせは、別の接続（今までどおり使い回す）
            await nep.changes(1, wait=0)
            self.assertEqual(server.connections, 3)
            self.assertEqual(server.requests[0], "/v1/changes?after=1&limit=500&wait=25")

    async def test_silence_is_given_up_soon_and_the_next_try_uses_a_new_connection(self):
        """接続が黙って死んだら、待つ長さ＋余裕であきらめて、やり直しは新しい接続で行う。"""
        async with FakeServer(["hang", page(rows=[], last_seq=4)]) as server:
            nep = await self.client(server)
            with unittest.mock.patch("nepsync.neplector.WAIT_SLACK_SECONDS", 0.2):
                started = asyncio.get_running_loop().time()
                with self.assertRaises(Transient) as caught:
                    await nep.changes(4, wait=1)
                self.assertLess(asyncio.get_running_loop().time() - started, 2.0)
                self.assertEqual(str(caught.exception), "/v1/changes TimeoutError")
                self.assertEqual(await nep.changes(4, wait=1), {"rows": [], "last_seq": 4})
            self.assertEqual(server.connections, 2)

    async def test_close_closes_both_connections(self):
        async with FakeServer([]) as server:
            nep = Neplector(server.url, KEY)
            await nep.changes(1)
            await nep.latest_seq()
            await nep.close()
            await asyncio.sleep(0.05)
            self.assertEqual((server.connections, server.closed_by_client), (2, 2))

    def test_why_uses_only_the_kind_for_other_errors(self):
        self.assertEqual(why(OSError(5, "架空の住所 example.invalid")), "OSError errno=5")
        self.assertEqual(why(ValueError("本文かもしれない値")), "ValueError")
        self.assertEqual(why(Transient("/v1/changes TimeoutError")), "Transient（/v1/changes TimeoutError）")
