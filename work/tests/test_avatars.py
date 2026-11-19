"""アイコンの絵文字のテスト。データはすべて架空。"""

import unittest

from nepsync.avatars import AvatarEmojis, sized_url
from nepsync.neplector import Transient
from nepsync.store import Store

from .fakes import GUILD_A, GUILD_B, FakeDiscord, fake_download, forbidden, make_world, memory_store

URL = "https://cdn.discordapp.com/avatars/501/abc.png"


def avatars(downloads=None, discord=None, store=None):
    discord = discord or FakeDiscord()
    store = store or memory_store()
    fetched = downloads if downloads is not None else []
    async def download(url):
        fetched.append(url)
        return b"png-bytes"
    made = AvatarEmojis(discord.client, store, {GUILD_A: "a", GUILD_B: "b"}, download, log=lambda text: None)
    return made, discord, store, fetched


class AvatarEmojiTest(unittest.IsolatedAsyncioTestCase):
    async def test_creates_once_and_reuses(self):
        made, discord, _, fetched = avatars()
        first = await made.emoji_for(GUILD_A, "501", URL)
        second = await made.emoji_for(GUILD_A, "501", URL)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^<:nep_501_a:\d+>$")
        self.assertEqual(len(fetched), 1)
        self.assertEqual(len(discord.app_emojis), 1)

    async def test_downloads_a_small_png(self):
        made, _, _, fetched = avatars()
        await made.emoji_for(GUILD_A, "501", "https://cdn.discordapp.com/avatars/501/a_abc.gif?size=1024")
        self.assertEqual(fetched, ["https://cdn.discordapp.com/avatars/501/a_abc.png?size=128"])

    async def test_changed_icon_replaces_the_old_emoji(self):
        made, discord, _, _ = avatars()
        first = await made.emoji_for(GUILD_A, "501", URL)
        second = await made.emoji_for(GUILD_A, "501", "https://cdn.discordapp.com/avatars/501/new.png")
        self.assertNotEqual(first, second)
        self.assertEqual(len(discord.app_emojis), 1)

    async def test_each_server_has_its_own(self):
        made, discord, _, _ = avatars()
        self.assertIn("nep_501_a", await made.emoji_for(GUILD_A, "501", URL))
        self.assertIn("nep_501_b", await made.emoji_for(GUILD_B, "501", URL))
        self.assertEqual(len(discord.app_emojis), 2)

    async def test_leftover_with_the_same_name_is_replaced(self):
        made, discord, _, _ = avatars()
        await discord.client.create_application_emoji(name="nep_501_a", image=b"old")
        self.assertRegex(await made.emoji_for(GUILD_A, "501", URL), r"^<:nep_501_a:\d+>$")
        self.assertEqual(len(discord.app_emojis), 1)

    async def test_no_icon_or_bad_input_gives_nothing(self):
        made, discord, _, _ = avatars()
        self.assertEqual(await made.emoji_for(GUILD_A, "501", None), "")
        self.assertEqual(await made.emoji_for("300", "501", URL), "")
        self.assertEqual(await made.emoji_for(GUILD_A, "abc", URL), "")
        self.assertEqual(discord.app_emojis, {})

    async def test_too_large_gives_nothing(self):
        discord, store = FakeDiscord(), memory_store()
        async def large(url):
            return b"x" * (256 * 1024 + 1)
        made = AvatarEmojis(discord.client, store, {GUILD_A: "a"}, large, log=lambda text: None)
        self.assertEqual(await made.emoji_for(GUILD_A, "501", URL), "")

    async def test_failure_gives_nothing_and_does_not_raise(self):
        made, discord, _, _ = avatars()
        discord.fail("create_application_emoji", forbidden(50013, "Missing Permissions"))
        self.assertEqual(await made.emoji_for(GUILD_A, "501", URL), "")

        async def broken(url):
            raise Transient("x")
        made.download = broken
        self.assertEqual(await made.emoji_for(GUILD_A, "502", URL), "")

    async def test_remove_user_and_prune(self):
        made, discord, store, _ = avatars()
        await made.emoji_for(GUILD_A, "501", URL)
        await made.emoji_for(GUILD_B, "501", URL)
        await made.emoji_for(GUILD_A, "502", URL)
        await made.remove_user("501")
        self.assertEqual([row["user_id"] for row in store.all_avatar_emojis()], ["502"])
        await made.prune(set())
        self.assertEqual(store.all_avatar_emojis(), [])
        self.assertEqual(discord.app_emojis, {})

    async def test_only_ids_and_a_hash_are_stored(self):
        made, _, store, _ = avatars()
        await made.emoji_for(GUILD_A, "501", URL)
        dump = "\n".join(store.db.iterdump())
        self.assertNotIn("abc.png", dump)

    async def test_unused_for_too_long_is_removed_and_made_again_when_needed(self):
        now = [1_000_000.0]
        discord, store = FakeDiscord(), memory_store()
        made = AvatarEmojis(discord.client, store, {GUILD_A: "a"}, fake_download, log=lambda text: None, keep_days=90, clock=lambda: now[0])
        await made.emoji_for(GUILD_A, "501", URL)
        await made.emoji_for(GUILD_A, "502", URL)
        now[0] += 60 * 86400
        await made.emoji_for(GUILD_A, "502", URL)          # 502 だけ、また表示された
        now[0] += 31 * 86400
        await made.prune({(GUILD_A, "501"), (GUILD_A, "502")})
        self.assertEqual([row["user_id"] for row in store.all_avatar_emojis()], ["502"])
        self.assertEqual(len(discord.app_emojis), 1)
        self.assertRegex(await made.emoji_for(GUILD_A, "501", URL), r"^<:nep_501_a:\d+>$")   # 要るときにまた作る

    async def test_kept_while_within_the_days(self):
        now = [1_000_000.0]
        discord, store = FakeDiscord(), memory_store()
        made = AvatarEmojis(discord.client, store, {GUILD_A: "a"}, fake_download, log=lambda text: None, keep_days=90, clock=lambda: now[0])
        await made.emoji_for(GUILD_A, "501", URL)
        now[0] += 89 * 86400
        await made.prune({(GUILD_A, "501")})
        self.assertEqual(len(store.all_avatar_emojis()), 1)

    async def test_old_file_gets_the_new_column_counting_from_now(self):
        import os
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE avatar_emojis (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, emoji_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (guild_id, user_id))")
            old.execute("INSERT INTO avatar_emojis VALUES ('100', '501', 'nep_501_a', '1', 'x')")
            old.commit()
            old.close()
            store = Store(path)
            self.assertGreater(store.all_avatar_emojis()[0]["last_used"], 1_700_000_000)
            store.db.close()

    async def test_sized_url(self):
        self.assertEqual(sized_url("https://cdn.discordapp.com/embed/avatars/0.png"), "https://cdn.discordapp.com/embed/avatars/0.png?size=128")


class EngineAvatarTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, _ = make_world("both")
        self.engine.avatars, _, _, _ = avatars(discord=self.discord, store=self.engine.store)

    async def test_opted_out_person_loses_their_icon(self):
        await self.engine.avatars.emoji_for(GUILD_A, "501", URL)
        self.nep.forget_rows = [{"seq": 1, "kind": "user", "id": "501"}]
        await self.engine.process_forget()
        self.assertEqual(self.discord.app_emojis, {})

    async def test_person_who_left_the_server_loses_their_icon(self):
        await self.engine.avatars.emoji_for(GUILD_A, "501", URL)
        await self.engine.avatars.emoji_for(GUILD_A, "888", URL)   # 名簿にいない
        await self.engine.process_forget()
        self.assertEqual([row["user_id"] for row in self.engine.store.all_avatar_emojis()], ["501"])


if __name__ == "__main__":
    unittest.main()
