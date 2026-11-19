"""メンションの通知と、スラッシュコマンドの名簿のテスト。データはすべて架空。"""

import unittest

from nepsync.neplector import Transient
from nepsync.avatars import AvatarEmojis
from nepsync.commands import Commands

from .fakes import NOW, GUILD_A, GUILD_B, copy_of, fake_download, forbidden, make_world


async def started(direction="both"):
    engine, nep, discord, notices = make_world(direction)
    await engine.sync_structure()
    engine.store.set("changes_after", nep.last_seq())
    return engine, nep, discord, notices


async def post(engine, nep, discord, content, channel="111", guild=GUILD_A, **extra):
    nep.say(str(len(nep.rows) + 800000), channel, guild, content, **extra)
    rows = [row for row in nep.rows if row["seq"] > int(engine.store.get("changes_after") or 0)]
    await engine.handle_batch(rows)
    return list(discord.messages.values())[-1]


class MentionTest(unittest.IsolatedAsyncioTestCase):
    async def test_person_only_on_the_other_server_is_notified(self):
        engine, nep, discord, _ = await started()
        sent = await post(engine, nep, discord, "<@502> 見てください")   # 502 は B にだけいる
        self.assertIn("<@502>", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "users": ["502"]})

    async def test_person_on_both_servers_is_shown_but_not_notified_twice(self):
        engine, nep, discord, _ = await started()
        nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": "501", "display_name": "架空さん", "avatar_url": None})
        sent = await post(engine, nep, discord, "<@501> へ")
        self.assertIn("<@501>", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": []})

    async def test_person_only_on_this_server_becomes_plain_text(self):
        engine, nep, discord, _ = await started()
        sent = await post(engine, nep, discord, "<@501> へ")
        self.assertIn("@架空さん", sent["content"])
        self.assertNotIn("<@", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": []})

    async def test_unknown_person_stays_unknown(self):
        engine, nep, discord, _ = await started()
        sent = await post(engine, nep, discord, "<@999999> へ")
        self.assertIn("@不明なメンバー", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": []})

    async def test_works_in_the_other_direction(self):
        engine, nep, discord, _ = await started()
        sent = await post(engine, nep, discord, "<@501> へ", channel=copy_of(engine, "111"), guild=GUILD_B, author="502")
        self.assertIn("<@501>", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "users": ["501"]})

    async def test_everyone_and_roles_are_never_notified(self):
        engine, nep, discord, _ = await started()
        nep.tables["roles"].append({"role_id": "70", "name": "企画"})
        sent = await post(engine, nep, discord, "@everyone <@&70> <@502>")
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "users": ["502"]})
        self.assertIn("@企画", sent["content"])

    async def test_a_quote_never_notifies(self):
        engine, nep, discord, _ = await started()
        await post(engine, nep, discord, "<@502> 元の発言")
        engine.store.set("changes_after", nep.last_seq())
        first_id = next(iter(nep.bodies))
        sent = await post(engine, nep, discord, "返事", reply_to_id=first_id)
        self.assertEqual(sent["allowed_mentions"], {"parse": []})
        self.assertNotIn("<@502>", sent["content"])

    async def test_editing_does_not_notify_again(self):
        engine, nep, discord, _ = await started()
        await post(engine, nep, discord, "<@502> 前")
        message_id = next(iter(nep.bodies))
        copy_id = engine.store.message_by_source(message_id)["copy_id"]
        engine.store.set("changes_after", nep.last_seq())
        nep.bodies[message_id].update(content="<@502> 後", edited_at="2026-11-09T12:00:00+00:00")
        nep.event("message.update", message_id=message_id, channel_id="111")
        await engine.handle_batch([nep.rows[-1]])
        self.assertIn("後", discord.messages[copy_id]["content"])
        self.assertEqual(discord.messages[copy_id]["allowed_mentions"], {"parse": []})

    async def test_one_way_also_notifies_on_the_target(self):
        engine, nep, discord, _ = await started("a_to_b")
        sent = await post(engine, nep, discord, "<@502> へ")
        self.assertEqual(sent["allowed_mentions"]["users"], ["502"])


def make_commands(engine, nep, discord, notices):
    return Commands(engine.config, nep, discord.client, engine.store, log=notices.append, clock=NOW.timestamp)


class HideTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await started()
        self.commands = make_commands(self.engine, self.nep, self.discord, self.notices)

    async def run_command(self, name, user="501"):
        return self.commands.visibility(user, hide=name == "nepsync-hide")

    async def names(self):
        """両方のサーバーから見た名簿（名簿に出るのは相手のサーバーの人だけなので、両方から見て合わせる）。"""
        found = []
        self.commands.roster.render = lambda cells: found.extend(cell["name"] for cell in cells) or b"png"
        await self.commands.members("111", 0, GUILD_A, "900")
        await self.commands.members(copy_of(self.engine, "111"), 0, GUILD_B, "900")
        return sorted(found)

    async def test_hide_removes_only_that_person(self):
        self.assertIn("載らないようにしました", await self.run_command("nepsync-hide"))
        self.assertEqual(await self.names(), ["相手さん"])

    async def test_show_puts_the_person_back(self):
        await self.run_command("nepsync-hide")
        await self.run_command("nepsync-show")
        self.assertIn("架空さん", await self.names())

    async def test_hiding_works_for_people_from_the_other_server_too(self):
        await self.run_command("nepsync-hide", user="502")
        self.assertNotIn("相手さん", await self.names())

    async def test_hide_twice_and_show_without_hide_are_harmless(self):
        await self.run_command("nepsync-hide")
        await self.run_command("nepsync-hide")
        await self.run_command("nepsync-show")
        await self.run_command("nepsync-show")
        self.assertIn("架空さん", await self.names())

    async def test_hiding_does_not_change_mentions_or_sync(self):
        await self.run_command("nepsync-hide", user="502")
        sent = await post(self.engine, self.nep, self.discord, "<@502> へ")
        self.assertEqual(sent["allowed_mentions"]["users"], ["502"])

    async def test_only_the_id_is_stored(self):
        await self.run_command("nepsync-hide")
        dump = "\n".join(self.engine.store.db.iterdump())
        self.assertIn("501", dump)
        self.assertNotIn("架空さん", dump)

    async def test_leaving_neplector_forgets_the_setting(self):
        await self.run_command("nepsync-hide")
        self.nep.forget_rows = [{"seq": 1, "kind": "user", "id": "501"}]
        await self.engine.process_forget()
        self.assertEqual(self.engine.store.hidden_users(), set())

    async def test_save_failure_is_answered(self):
        def broken(user_id, hidden):
            raise RuntimeError("disk")
        self.engine.store.set_hidden = broken
        self.assertIn("保存できませんでした", await self.run_command("nepsync-hide"))

    async def test_missing_user_is_ignored(self):
        self.assertIsNone(await self.run_command("nepsync-hide", user=""))
        self.assertEqual(self.engine.store.hidden_users(), set())


class ReactorsMenuTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await started()
        self.commands = make_commands(self.engine, self.nep, self.discord, self.notices)
        self.nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": "503", "display_name": "相手の次郎", "avatar_url": "https://example.invalid/j.png"})
        await post(self.engine, self.nep, self.discord, "架空の発言")
        self.source_id = next(iter(self.nep.bodies))
        self.copy_id = self.engine.store.message_by_source(self.source_id)["copy_id"]
        self.nep.bodies[self.copy_id] = {"message_id": self.copy_id, "guild_id": GUILD_B, "channel_id": copy_of(self.engine, "111"), "reactions": [], "webhook_id": "7"}

    async def react(self, message_id, emoji, user):
        self.nep.bodies[message_id]["reactions"].append({"emoji": emoji, "user_id": user, "burst": 0})

    async def ask(self, message_id, guild=GUILD_A):
        return await self.commands.reactors(message_id, guild)

    async def text(self, message_id, guild=GUILD_A):
        return (await self.ask(message_id, guild))["content"]

    async def test_grouped_by_emoji_with_people_under_it(self):
        await self.react(self.copy_id, "😀", "502")
        await self.react(self.copy_id, "😀", "503")
        await self.react(self.source_id, "😀", "501")
        await self.react(self.copy_id, "👍", "502")
        lines = (await self.text(self.source_id)).split("\n")
        self.assertEqual(lines[1:], ["😀　3人", f"　サーバー{GUILD_A}：架空さん", f"　サーバー{GUILD_B}：相手さん・相手の次郎",
                                     "👍　1人", f"　サーバー{GUILD_B}：相手さん"])

    async def test_own_server_comes_first(self):
        await self.react(self.source_id, "😀", "501")
        await self.react(self.copy_id, "😀", "502")
        lines = (await self.text(self.copy_id, guild=GUILD_B)).split("\n")
        self.assertEqual(lines[2:4], [f"　サーバー{GUILD_B}：相手さん", f"　サーバー{GUILD_A}：架空さん"])

    async def test_asking_on_the_copy_works_too(self):
        await self.react(self.source_id, "👍", "501")
        self.assertIn("架空さん", await self.text(self.copy_id, guild=GUILD_B))

    async def test_custom_emoji_is_grouped_by_name_and_shown_for_the_asker(self):
        self.nep.tables["emojis"].append({"emoji_id": "9001", "guild_id": GUILD_A, "name": "logo", "animated": 0, "available": 1})
        await self.react(self.source_id, "logo:9001", "501")
        await self.react(self.copy_id, "logo:9002", "502")
        await self.react(self.copy_id, "only_there:9003", "502")
        text = await self.text(self.source_id)
        self.assertEqual(text.count("logo"), 1)
        self.assertIn("<:logo:9001>　2人", text)
        self.assertIn(":only_there:　1人", text)

    async def test_icons_are_shown_before_names(self):
        self.commands.avatars = AvatarEmojis(self.discord.client, self.engine.store, {GUILD_A: "a", GUILD_B: "b"}, fake_download, log=lambda text: None)
        await self.react(self.copy_id, "😀", "503")
        await self.react(self.copy_id, "😀", "502")
        line = (await self.text(self.source_id)).split("\n")[2]
        self.assertRegex(line, r"^　サーバー200：<:nep_503_b:\d+>相手の次郎・相手さん$")   # 付けた順。502 はアイコンが無い

    async def test_icon_failure_still_shows_the_name(self):
        self.discord.fail("create_application_emoji", forbidden(50013, "Missing Permissions"))
        self.commands.avatars = AvatarEmojis(self.discord.client, self.engine.store, {GUILD_A: "a", GUILD_B: "b"}, fake_download, log=lambda text: None)
        await self.react(self.copy_id, "😀", "503")
        self.assertIn("　サーバー200：相手の次郎", await self.text(self.source_id))

    async def test_no_buttons_and_no_embeds(self):
        await self.react(self.copy_id, "😀", "502")
        body = await self.ask(self.source_id)
        self.assertEqual(body["embeds"], [])
        self.assertIsNone(body["pager"])

    async def test_long_lists_stay_within_the_limit(self):
        for number in range(400):
            user = f"7{number:03d}"
            self.nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": user, "display_name": f"とても長い名前の人{number}", "avatar_url": None})
            await self.react(self.copy_id, "😀", user)
        self.assertLessEqual(len(await self.text(self.source_id)), 2000)

    async def test_no_reactions(self):
        self.assertIn("まだ", await self.text(self.source_id))

    async def test_unsynced_message(self):
        self.assertIn("同期していない", (await self.ask("424242"))["content"])

    async def test_opted_out_message_is_not_shown(self):
        del self.nep.bodies[self.source_id]
        self.assertIn("表示できません", (await self.ask(self.copy_id, guild=GUILD_B))["content"])

    async def test_failure_gives_a_message(self):
        original = self.nep.messages

        def down(ids):
            raise Transient("x")
        self.nep.messages = down
        self.assertIn("作れませんでした", (await self.ask(self.source_id))["content"])
        self.nep.messages = original


if __name__ == "__main__":
    unittest.main()
