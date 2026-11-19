"""告知チャンネルの「相手のサーバーにも出す」「下げる」のテスト（ADR 0009）。データはすべて架空。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from nepsync import bot
from nepsync.commands import SHARE, WITHDRAW
from nepsync.config import ConfigError, load_config

from .fakes import GUILD_A, GUILD_B, NOW, copy_of, make_world
from .test_bot import FakeInteraction, make_bot
from .test_mentions_commands import make_commands

ANN_A, ANN_B = "130", "230"
EVERYONE = str((1 << 10) | (1 << 11))


async def world(**options):
    engine, nep, discord, notices = make_world("both", announcements=((ANN_A, ANN_B),), announce_ping_roles=("member",), **options)
    discord.add(ANN_A, GUILD_A, 5, "お知らせ")
    discord.add(ANN_B, GUILD_B, 5, "お知らせ")
    discord.roles[GUILD_A] = [{"id": GUILD_A, "name": "@everyone", "permissions": EVERYONE}, {"id": "171", "name": "member", "permissions": "0"},
                              {"id": "172", "name": "運営", "permissions": "0"}]
    discord.roles[GUILD_B] = [{"id": GUILD_B, "name": "@everyone", "permissions": EVERYONE}, {"id": "271", "name": "member", "permissions": "0"}]
    nep.tables["roles"].append({"role_id": "172", "name": "運営"})
    await engine.sync_structure()
    engine.store.set("changes_after", nep.last_seq())
    return engine, nep, discord, notices


async def feed(engine, nep):
    rows = [row for row in nep.rows if row["seq"] > int(engine.store.get("changes_after") or 0)]
    await engine.handle_batch(rows)


async def request(engine, message_id, action=SHARE, channel=ANN_A, at=None):
    engine.store.request_announce(message_id, channel, action, NOW.timestamp() if at is None else at)
    await engine.process_announcements()
    return engine.store.announce_request(message_id)


def posts_in(discord, channel_id):
    return [item for item in discord.messages.values() if item["channel_id"] == channel_id]


class ShareTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await world()

    async def test_announcements_are_not_copied_by_default(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await feed(self.engine, self.nep)
        self.assertEqual(posts_in(self.discord, ANN_B), [])
        self.assertIsNone(self.engine.store.message_by_source("9001"))

    async def test_share_posts_the_copy_and_answers_with_its_link(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        row = await request(self.engine, "9001")
        copies = posts_in(self.discord, ANN_B)
        self.assertEqual(len(copies), 1)
        self.assertIn("架空の告知", copies[0]["content"])
        self.assertEqual(row["result"], "shared")
        self.assertEqual(row["link"], f"https://discord.com/channels/{GUILD_B}/{ANN_B}/{copies[0]['id']}")

    async def test_links_to_synced_messages_point_to_the_other_server(self):
        self.nep.say("8001", "111", GUILD_A, "架空の発言")
        await feed(self.engine, self.nep)
        copy_id = self.engine.store.message_by_source("8001")["copy_id"]
        self.nep.say("9001", ANN_A, GUILD_A, f"詳しくは https://discord.com/channels/{GUILD_A}/111/8001")
        await request(self.engine, "9001")
        body = posts_in(self.discord, ANN_B)[0]["content"]
        self.assertIn(f"https://discord.com/channels/{GUILD_B}/{copy_of(self.engine, '111')}/{copy_id}", body)
        self.assertNotIn(f"/channels/{GUILD_A}/", body)

    async def test_only_the_member_role_is_notified(self):
        self.nep.say("9001", ANN_A, GUILD_A, "<@&171> <@&172> @everyone 集合")
        await request(self.engine, "9001")
        sent = posts_in(self.discord, ANN_B)[0]
        self.assertIn("<@&271>", sent["content"])
        self.assertIn("@運営", sent["content"])
        self.assertNotIn("<@&172>", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "roles": ["271"]})

    async def test_member_role_missing_on_the_other_server_is_not_notified(self):
        self.discord.roles[GUILD_B] = [item for item in self.discord.roles[GUILD_B] if item["name"] != "member"]
        self.nep.tables["roles"].append({"role_id": "171", "name": "member"})
        self.nep.say("9001", ANN_A, GUILD_A, "<@&171> 集合")
        await request(self.engine, "9001")
        sent = posts_in(self.discord, ANN_B)[0]
        self.assertIn("@member", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": []})

    async def test_synced_channels_still_never_notify_roles(self):
        sent_before = len(self.discord.messages)
        self.nep.say("8001", "111", GUILD_A, "<@&171> へ")
        await feed(self.engine, self.nep)
        self.assertEqual(len(self.discord.messages), sent_before + 1)
        sent = list(self.discord.messages.values())[-1]
        self.assertNotIn("<@&", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": []})

    async def test_sharing_twice_posts_once(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await request(self.engine, "9001")
        row = await request(self.engine, "9001")
        self.assertEqual(len(posts_in(self.discord, ANN_B)), 1)
        self.assertEqual(row["result"], "shared")

    async def test_works_from_the_other_server_too(self):
        self.nep.say("9002", ANN_B, GUILD_B, "相手の架空の告知", author="502")
        row = await request(self.engine, "9002", channel=ANN_B)
        self.assertEqual(row["result"], "shared")
        self.assertEqual(len(posts_in(self.discord, ANN_A)), 1)

    async def test_copy_is_never_shared_back(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await request(self.engine, "9001")
        copy_id = posts_in(self.discord, ANN_B)[0]["id"]
        row = await request(self.engine, copy_id, channel=ANN_B)
        self.assertEqual(row["result"], "is_copy")
        self.assertEqual(posts_in(self.discord, ANN_A), [])

    async def test_waits_for_neplector_then_gives_up(self):
        row = await request(self.engine, "9009")
        self.assertIsNone(row["result"])
        row = await request(self.engine, "9009", at=NOW.timestamp() - 601)
        self.assertEqual(row["result"], "no_record")

    async def test_message_without_body_is_not_shared(self):
        self.nep.say("9001", ANN_A, GUILD_A, "x")
        self.nep.bodies["9001"]["content"] = None
        self.assertEqual((await request(self.engine, "9001"))["result"], "no_body")
        self.assertEqual(posts_in(self.discord, ANN_B), [])

    async def test_other_channels_are_refused(self):
        self.nep.say("8001", "111", GUILD_A, "架空の発言")
        self.assertEqual((await request(self.engine, "8001", channel="111"))["result"], "not_announcement")

    async def test_edit_reaches_the_copy(self):
        self.nep.say("9001", ANN_A, GUILD_A, "<@&171> 架空の告知")
        await request(self.engine, "9001")
        self.nep.bodies["9001"].update(content="<@&171> 直した告知", edited_at="2026-11-09T12:01:00+00:00")
        self.nep.event("message.update", message_id="9001", channel_id=ANN_A)
        await feed(self.engine, self.nep)
        copy = posts_in(self.discord, ANN_B)[0]
        self.assertIn("直した告知", copy["content"])
        self.assertIn("<@&271>", copy["content"])
        self.assertEqual(copy["allowed_mentions"], {"parse": []})   # 編集では鳴らさない

    async def test_old_requests_are_forgotten(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await request(self.engine, "9001", at=NOW.timestamp() - 90000)
        self.assertIsNone(self.engine.store.announce_request("9001"))


class DeadLinkTest(unittest.IsolatedAsyncioTestCase):
    """相手のサーバーで開けないリンクを、押した人に知らせる（ADR 0009 判断5）。"""

    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await world()
        self.commands = make_commands(self.engine, self.nep, self.discord, self.notices)

    async def share(self, content):
        self.nep.say("9001", ANN_A, GUILD_A, content)
        self.commands.announce(SHARE, "9001", ANN_A, "501", "501", False)
        await self.engine.process_announcements()
        return self.commands.announce_result("9001")

    async def test_unsynced_message_is_warned_but_still_shared(self):
        link = f"https://discord.com/channels/{GUILD_A}/111/7777"   # 同期チャンネルだが、写していない発言
        text = await self.share(f"詳しくは {link}")
        self.assertTrue(text.startswith("相手のサーバーに出しました"))
        self.assertIn("⚠️", text)
        self.assertIn(f"<{link}>", text)
        self.assertIn("チャンネルへのリンクにしました", text)
        self.assertEqual(len(posts_in(self.discord, ANN_B)), 1)

    async def test_unpaired_channel_is_warned(self):
        link = f"https://discord.com/channels/{GUILD_A}/119/7777"   # 同期していないチャンネル
        text = await self.share(f"詳しくは {link}")
        self.assertIn("元のサーバーのリンクのまま", text)

    async def test_synced_message_and_other_servers_are_not_warned(self):
        self.nep.say("8001", "111", GUILD_A, "架空の発言")
        await feed(self.engine, self.nep)
        text = await self.share(f"https://discord.com/channels/{GUILD_A}/111/8001 と https://discord.com/channels/300/301/302")
        self.assertNotIn("⚠️", text)

    async def test_warning_keeps_only_ids_in_the_store(self):
        await self.share(f"秘密っぽい架空の文 https://discord.com/channels/{GUILD_A}/119/7777")
        warning = self.engine.store.announce_request("9001")["warning"]
        self.assertNotIn("秘密", warning)

    async def test_many_links_are_cut_short(self):
        links = " ".join(f"https://discord.com/channels/{GUILD_A}/119/{7000 + index}" for index in range(15))
        text = await self.share(links)
        self.assertIn("リンクが 15 個", text)
        self.assertIn("ほか 5 個", text)
        self.assertLessEqual(len(text), 2000)


class ReactionTest(unittest.IsolatedAsyncioTestCase):
    """出した告知のリアクションは、同期チャンネルと同じく両方向に写る。"""

    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await world()
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await request(self.engine, "9001")
        self.copy_id = posts_in(self.discord, ANN_B)[0]["id"]
        self.nep.bodies[self.copy_id] = {"message_id": self.copy_id, "channel_id": ANN_B, "reactions": [], "poll": None, "webhook_id": "7"}

    async def react(self, message_id, emoji, user):
        self.nep.bodies[message_id]["reactions"].append({"emoji": emoji, "user_id": user, "burst": 0})
        self.nep.event("reaction.add", message_id=message_id, emoji=emoji, user_id=user)
        await feed(self.engine, self.nep)

    async def test_reaction_on_the_original_reaches_the_copy(self):
        await self.react("9001", "👍", "501")
        self.assertIn(("add_reaction", GUILD_B, self.copy_id, "👍"), self.discord.writes)

    async def test_reaction_on_the_copy_reaches_the_original(self):
        await self.react(self.copy_id, "🎉", "502")
        self.assertIn(("add_reaction", GUILD_A, "9001", "🎉"), self.discord.writes)

    async def test_removed_reaction_is_taken_back(self):
        await self.react("9001", "👍", "501")
        self.nep.bodies["9001"]["reactions"] = []
        self.nep.event("reaction.remove", message_id="9001", emoji="👍", user_id="501")
        await feed(self.engine, self.nep)
        self.assertIn(("remove_reaction", GUILD_B, self.copy_id, "👍"), self.discord.writes)

    async def test_unshared_announcement_reactions_are_ignored(self):
        self.nep.say("9002", ANN_A, GUILD_A, "出していない架空の告知")
        await self.react("9002", "👍", "501")
        self.assertFalse(any(item[0] == "add_reaction" for item in self.discord.writes))


class WithdrawTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await world()
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await request(self.engine, "9001")
        self.copy_id = posts_in(self.discord, ANN_B)[0]["id"]

    async def test_withdraw_deletes_only_the_copy(self):
        row = await request(self.engine, "9001", action=WITHDRAW)
        self.assertEqual(row["result"], "withdrawn")
        self.assertEqual(posts_in(self.discord, ANN_B), [])
        self.assertIsNone(self.engine.store.message_by_source("9001"))
        self.assertNotIn(("delete_message",), [item[:1] for item in self.discord.writes if item[2] == "9001"])

    async def test_withdraw_from_the_copy_side(self):
        row = await request(self.engine, self.copy_id, action=WITHDRAW, channel=ANN_B)
        self.assertEqual(row["result"], "withdrawn")
        self.assertEqual(posts_in(self.discord, ANN_B), [])

    async def test_can_share_again_after_withdrawing(self):
        await request(self.engine, "9001", action=WITHDRAW)
        self.assertEqual((await request(self.engine, "9001"))["result"], "shared")
        self.assertEqual(len(posts_in(self.discord, ANN_B)), 1)

    async def test_deleting_the_original_deletes_the_copy(self):
        self.nep.event("message.delete", message_id="9001", channel_id=ANN_A)
        await feed(self.engine, self.nep)
        self.assertEqual(posts_in(self.discord, ANN_B), [])


class CommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await world()
        self.commands = make_commands(self.engine, self.nep, self.discord, self.notices)

    def ask(self, action=SHARE, message="9001", channel=ANN_A, user="501", author="501", manage=False):
        return self.commands.announce(action, message, channel, user, author, manage)

    async def test_author_and_managers_can_ask(self):
        self.assertIsNone(self.ask(user="501", author="501"))
        self.assertIsNone(self.ask(message="9002", user="777", author="501", manage=True))
        self.assertEqual(len(self.engine.store.pending_announces()), 2)

    async def test_others_are_refused(self):
        self.assertIn("メッセージの管理", self.ask(user="777", author="501"))
        self.assertEqual(self.engine.store.pending_announces(), [])

    async def test_only_announcement_channels(self):
        self.assertIn("告知チャンネル", self.ask(channel="111"))

    async def test_copy_author_is_the_original_writer(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        await request(self.engine, "9001")
        copy_id = posts_in(self.discord, ANN_B)[0]["id"]
        self.assertIn("写し", self.ask(message=copy_id, channel=ANN_B, user="501", author="12345"))
        self.assertIsNone(self.ask(WITHDRAW, message=copy_id, channel=ANN_B, user="501", author="12345"))
        self.assertIn("メッセージの管理", self.ask(WITHDRAW, message=copy_id, channel=ANN_B, user="777", author="12345"))

    async def test_withdraw_of_unshared_is_answered_at_once(self):
        self.assertIn("出ていません", self.ask(WITHDRAW))

    async def test_result_includes_the_link(self):
        self.nep.say("9001", ANN_A, GUILD_A, "架空の告知")
        self.ask()
        self.assertIsNone(self.commands.announce_result("9001"))
        await self.engine.process_announcements()
        text = self.commands.announce_result("9001")
        self.assertTrue(text.startswith("相手のサーバーに出しました\nhttps://discord.com/channels/"))


class MenuTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await world()
        self.logic = make_commands(self.engine, self.nep, self.discord, self.notices)
        self.client = make_bot(self.engine, self.logic, self.notices)
        self.pause, bot.ANNOUNCE_POLL_SECONDS = bot.ANNOUNCE_POLL_SECONDS, 0

    async def asyncTearDown(self):
        bot.ANNOUNCE_POLL_SECONDS = self.pause
        await self.client.close()

    async def test_menus_are_added_when_announcements_are_set(self):
        import discord
        found = self.client.tree.get_commands(guild=discord.Object(id=int(GUILD_A)))
        self.assertEqual([item.name for item in found][-2:], ["相手のサーバーにも出す", "相手のサーバーから下げる"])

    async def test_share_answers_privately_with_the_link_and_ignores_the_given_body(self):
        self.nep.say("9001", ANN_A, GUILD_A, "Neplector の架空の本文")
        interaction = FakeInteraction(channel_id=ANN_A, user="501")
        interaction.permissions = SimpleNamespace(manage_messages=False)
        message = SimpleNamespace(id=9001, author=SimpleNamespace(id=501), content="メニューと一緒に届いた架空の本文")
        await self.client.handle_announce(interaction, message, SHARE)
        self.assertEqual(interaction.calls[0], ("defer", {"ephemeral": True, "thinking": True}))
        self.assertIn(f"https://discord.com/channels/{GUILD_B}/{ANN_B}/", interaction.edited["content"])
        self.assertEqual(interaction.edited["allowed_mentions"].to_dict(), {"parse": []})
        copy = posts_in(self.discord, ANN_B)[0]
        self.assertIn("Neplector の架空の本文", copy["content"])
        self.assertNotIn("メニューと一緒に届いた", copy["content"])
        self.assertFalse(any("メニューと一緒に届いた" in text for text in self.notices))


class MenuWithoutAnnouncementsTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_menus_without_announcements(self):
        import discord
        engine, nep, fake, notices = make_world("both")
        client = make_bot(engine, make_commands(engine, nep, fake, notices), notices)
        names = [item.name for item in client.tree.get_commands(guild=discord.Object(id=int(GUILD_A)))]
        self.assertNotIn("相手のサーバーにも出す", names)
        await client.close()


class ConfigTest(unittest.IsolatedAsyncioTestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nepsync.toml"
            path.write_text(f'guild_a = "1"\nguild_b = "2"\ncategory_names = ["共同"]\n{extra}', encoding="utf-8")
            return load_config(path)

    async def test_pairs_and_roles(self):
        config = self.load('announcements = [["130", "230"]]\nannounce_ping_roles = ["member"]\n')
        self.assertEqual(config.announcements, (("130", "230"),))
        self.assertEqual(config.announce_side("230"), ("b", "130"))
        self.assertEqual(config.announce_ping_roles, ("member",))

    async def test_defaults_to_none(self):
        config = self.load("")
        self.assertEqual(config.announcements, ())
        self.assertIsNone(config.announce_side("130"))

    async def test_wrong_shape_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load('announcements = ["130", "230"]\n')


if __name__ == "__main__":
    unittest.main()
