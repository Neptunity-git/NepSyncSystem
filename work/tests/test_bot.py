"""discord.py の bot（接続・スラッシュコマンド）のテスト。Discord にはつながない。データはすべて架空。"""

import logging
import unittest
from types import SimpleNamespace

import discord

from nepsync import bot

from .fakes import GUILD_A, GUILD_B
from .test_mentions_commands import make_commands, started


def make_bot(engine, logic, notices):
    """本物の discord.py の Client（つながない）に、偽物の世界を見る engine と logic を入れる。"""
    client = bot.SyncBot(engine.config, notify=notices.append, log=notices.append)
    client.engine, client.logic = engine, logic
    return client


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))

    async def send_modal(self, modal):
        self.calls.append(("modal", modal))

    async def edit_message(self, **kwargs):
        self.calls.append(("edit_message", kwargs))


class FakeInteraction:
    def __init__(self, guild=GUILD_A, channel_id="111", channel=None, user="501", message=None):
        self.guild_id, self.channel_id, self.channel = int(guild), int(channel_id), channel
        self.user = SimpleNamespace(id=int(user))
        self.message = message
        self.calls = []
        self.response = FakeResponse(self.calls)

    async def edit_original_response(self, **kwargs):
        self.calls.append(("edit", kwargs))

    @property
    def edited(self):
        return [kwargs for kind, kwargs in self.calls if kind == "edit"][-1]

    def only_edit(self):
        """ボタン・メニューの答え：新しい返事を出さず、押された発言を書き換えただけ。"""
        assert [kind for kind, _ in self.calls] == ["edit_message"], self.calls
        return self.calls[0][1]


def message_from(edited, content=None):
    """bot が書いた発言を、Discord から届く形（本文とメニュー）にする。"""
    menus = [discord.SelectMenu(child.item.to_component_dict()) for child in edited["view"].children
             if isinstance(child.item, discord.ui.Select)]
    text = edited.get("content") if content is None else content
    return SimpleNamespace(content=text or "", components=[SimpleNamespace(children=menus)])


PRIVATE = ("defer", {"ephemeral": True, "thinking": True})


class CommandBotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await started()
        self.logic = make_commands(self.engine, self.nep, self.discord, self.notices)
        self.nep.tables["member_servers"].append({"guild_id": GUILD_A, "user_id": "777", "display_name": "発言していない人", "avatar_url": None})
        self.client = make_bot(self.engine, self.logic, self.notices)

    async def asyncTearDown(self):
        await self.client.close()

    def test_only_the_guilds_intent(self):
        """サーバーの形の知らせだけ受ける。発言・リアクション・メンバーの知らせと、発言の中身は受けない。"""
        self.assertEqual(bot.intents(), discord.Intents(guilds=True))
        self.assertEqual(self.client.intents, discord.Intents(guilds=True))
        for name in ("guild_messages", "message_content", "members", "guild_reactions", "presences", "dm_messages"):
            self.assertFalse(getattr(self.client.intents, name), name)

    def test_all_commands_only_on_the_two_servers(self):
        self.assertEqual(self.client.tree.get_commands(), [])
        for guild in (GUILD_A, GUILD_B):
            found = self.client.tree.get_commands(guild=discord.Object(id=int(guild)))
            self.assertEqual([item.name for item in found], ["nepsync-members", "nepsync-hide", "nepsync-show", "リアクションした人", "投票した人", "この人を呼ぶ"])
            self.assertEqual(found[-1].type, discord.AppCommandType.message)
            option = found[0].parameters[0]
            self.assertEqual((option.display_name, option.required, option.autocomplete is not None), ("名前", False, True))

    async def test_register_on_both_servers(self):
        synced = []

        async def sync(*, guild=None):
            synced.append(guild.id)
            return []
        self.client.tree.sync = sync
        self.assertTrue(await self.client.register())
        self.assertEqual(synced, [int(GUILD_A), int(GUILD_B)])

    async def test_register_failure_tells_how_to_fix(self):
        async def refused(*, guild=None):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50001, "message": "Missing Access"})
        self.client.tree.sync = refused
        self.assertFalse(await self.client.register())
        self.assertTrue(any("applications.commands" in item for item in self.notices))

    def add_b(self, count):
        for number in range(count):
            self.nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": f"60{number:02d}", "display_name": f"人{number:02d}", "avatar_url": None})

    async def test_members_answer_is_private_image_and_menu(self):
        interaction = FakeInteraction()
        await self.client.show_members(interaction)
        self.assertEqual(interaction.calls[0], PRIVATE)
        edited = interaction.edited
        self.assertEqual(edited["allowed_mentions"].to_dict(), {"parse": []})
        self.assertIsNone(edited["content"])   # 本文は、選んだ人の <@ID> だけにする（まだ誰も選んでいない）
        self.assertEqual(edited["embeds"], [])   # 説明の枠は出さない（場所を取らない）
        self.assertEqual([item.filename for item in edited["attachments"]], ["members.png"])
        menu = edited["view"].children[0].item
        self.assertEqual(menu.custom_id, "nepsync-pick:111:0:1")
        self.assertEqual([option.label for option in menu.options], ["1. 相手さん"])   # A の人は出ない
        self.assertEqual(len(edited["view"].children), 1)   # 1ページならページ送りは無い

    async def test_thread_uses_its_parent_channel(self):
        thread = SimpleNamespace(type=discord.ChannelType.public_thread, parent_id=111)
        interaction = FakeInteraction(channel_id="5000", channel=thread)
        await self.client.show_members(interaction)
        self.assertEqual(interaction.edited["view"].children[0].item.custom_id, "nepsync-pick:111:0:1")

    async def test_pages_and_buttons(self):
        self.add_b(30)
        interaction = FakeInteraction()
        await self.client.show_members(interaction)
        menu, first, second = (child.item for child in interaction.edited["view"].children)
        self.assertEqual(len(menu.options), 25)
        self.assertEqual(menu.max_values, 10)
        self.assertEqual((first.label, first.disabled, first.custom_id), ("前へ", True, "nepsync-members:111:-1"))
        self.assertEqual((second.label, second.disabled, second.custom_id), ("次へ", False, "nepsync-members:111:1"))

        match = bot.PageButton.__discord_ui_compiled_template__.fullmatch(second.custom_id)
        button = await bot.PageButton.from_custom_id(interaction, second, match)
        pressed = FakeInteraction()
        await self.client.turn_page(pressed, button.channel_id, button.page)
        self.assertEqual(pressed.calls[0], ("defer", {}))   # 今の発言を書き換える
        self.assertEqual(len(pressed.edited["view"].children[0].item.options), 6)
        self.assertTrue(pressed.edited["view"].children[2].item.disabled)

    async def test_choosing_edits_the_same_message_and_adds_up(self):
        self.add_b(30)
        interaction = FakeInteraction()
        await self.client.show_members(interaction)
        roster = message_from(interaction.edited)
        self.assertEqual(roster.content, "")

        pressed = FakeInteraction(message=roster)
        await self.client.choose(pressed, "111", 0, 2, ["6000"])
        edited = pressed.only_edit()
        self.assertEqual(edited["content"], "<@6000>")   # スマホ：長押しでコピー
        self.assertEqual([item.description for item in edited["embeds"]], ["```\n<@6000>\n```"])   # PC：コードブロック
        self.assertEqual(edited["allowed_mentions"].to_dict(), {"parse": []})
        items = [child.item for child in edited["view"].children]
        self.assertEqual([option.value for option in items[0].options if option.default], ["6000"])
        self.assertEqual([getattr(item, "label", None) for item in items[1:]], ["前へ", "次へ", "選び直す", "★ に入れる（名簿の先頭に出す）"])
        self.assertIn("6000", self.engine.store.picks("501"))

        pressed = FakeInteraction(message=message_from(edited))
        await self.client.choose(pressed, "111", 0, 2, ["6000", "6001"])
        edited = pressed.only_edit()
        self.assertEqual(edited["content"], "<@6000> <@6001>")
        self.assertNotIn("★", " ".join(str(getattr(child.item, "label", "")) for child in edited["view"].children))

        # ページを送っても、選んだ人は残る
        pressed = FakeInteraction(message=message_from(edited))
        await self.client.turn_page(pressed, "111", 1)
        self.assertEqual(pressed.edited["content"], "<@6000> <@6001>")
        page2 = message_from(pressed.edited)
        pressed = FakeInteraction(message=page2)
        await self.client.choose(pressed, "111", 1, 2, ["6025"])
        self.assertEqual(pressed.only_edit()["content"], "<@6000> <@6001> <@6025>")

        # このページで外すと、このページの人だけ外れる
        pressed = FakeInteraction(message=message_from(pressed.only_edit()))
        await self.client.choose(pressed, "111", 1, 2, [])
        self.assertEqual(pressed.only_edit()["content"], "<@6000> <@6001>")

    async def test_clear_empties_the_selection(self):
        interaction = FakeInteraction()
        await self.client.show_members(interaction)
        pressed = FakeInteraction(message=message_from(interaction.edited, content="<@502>"))
        await self.client.reselect(pressed, "111", 0, 1, [])
        edited = pressed.only_edit()
        self.assertIsNone(edited["content"])
        self.assertEqual(edited["embeds"], [])
        self.assertEqual([child.item.type for child in edited["view"].children], [discord.ComponentType.select])

    async def test_star_toggles_for_the_one_chosen(self):
        interaction = FakeInteraction()
        await self.client.show_members(interaction)
        roster = message_from(interaction.edited, content="<@502>")
        await self.client.toggle_star(FakeInteraction(message=roster), "111", 0, 1)
        self.assertEqual(self.engine.store.stars("501"), ["502"])
        pressed = FakeInteraction(message=roster)
        await self.client.toggle_star(pressed, "111", 0, 1)
        self.assertEqual(self.engine.store.stars("501"), [])
        self.assertEqual(pressed.only_edit()["view"].children[-1].item.label, "★ に入れる（名簿の先頭に出す）")

    async def test_old_and_new_ids_match(self):
        for item, custom_id in ((bot.PickMenu, "nepsync-pick:111:0:2"), (bot.ClearButton, "nepsync-clear:111:0:1"), (bot.StarButton, "nepsync-star:0:0:1")):
            self.assertIsNotNone(item.__discord_ui_compiled_template__.fullmatch(custom_id))

    async def test_search_hit_shows_mention_and_star_only(self):
        self.add_b(3)
        interaction = FakeInteraction()
        await self.client.show_members(interaction, "人01")
        edited = interaction.edited
        self.assertEqual(edited["content"], "<@6001>")
        self.assertEqual(edited["attachments"], [])
        self.assertEqual([child.item.custom_id for child in edited["view"].children], ["nepsync-star:0:0:1"])

    async def test_call_author_menu(self):
        interaction = FakeInteraction()
        await self.client.call_author(interaction, SimpleNamespace(id=123456))
        self.assertEqual(interaction.calls[0], PRIVATE)
        self.assertIn("写しにだけ", interaction.edited["content"])
        self.assertIsNone(interaction.edited["view"])

    async def test_buttons_from_before_still_match(self):
        self.assertIsNotNone(bot.PageButton.__discord_ui_compiled_template__.fullmatch("nepsync-members:111:-1"))
        self.assertIsNone(bot.PageButton.__discord_ui_compiled_template__.fullmatch("other:111:1"))

    async def test_other_servers_are_ignored(self):
        interaction = FakeInteraction(guild="300")
        await self.client.show_members(interaction)
        await self.client.set_visibility(interaction, hide=True)
        await self.client.show_list(interaction, SimpleNamespace(id=1), self.logic.reactors)
        await self.client.turn_page(interaction, "111", 1)
        await self.client.choose(interaction, "111", 0, 1, ["502"])
        await self.client.call_author(interaction, SimpleNamespace(id=1))
        self.assertEqual(interaction.calls, [])

    async def test_hide_is_private(self):
        interaction = FakeInteraction()
        await self.client.set_visibility(interaction, hide=True)
        self.assertEqual(interaction.calls[0], PRIVATE)
        self.assertIn("載らないようにしました", interaction.edited["content"])
        self.assertEqual(self.engine.store.hidden_users(), {"501"})

    async def test_reactors_answer_is_private_and_never_pings(self):
        interaction = FakeInteraction()
        await self.client.show_list(interaction, SimpleNamespace(id=424242), self.logic.reactors)
        self.assertEqual(interaction.calls[0], PRIVATE)
        self.assertIn("同期していない", interaction.edited["content"])
        self.assertEqual(interaction.edited["allowed_mentions"].to_dict(), {"parse": []})
        self.assertEqual(interaction.edited["embeds"], [])

    async def test_failure_is_logged_without_details(self):
        def broken(*args):
            raise RuntimeError("架空の本文")
        self.logic.members = broken
        await self.client.guard(self.client.show_members(FakeInteraction()))
        self.assertEqual(self.notices, ["コマンドに答えられませんでした: RuntimeError"])


class LibraryLogTest(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger("discord")
        saved = (logger.level, logger.propagate, list(logger.handlers))

        def restore():
            logger.setLevel(saved[0])
            logger.propagate = saved[1]
            logger.handlers[:] = saved[2]
        self.addCleanup(restore)

    def test_debug_logs_with_bodies_are_never_written(self):
        lines = []
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, logging.WARNING)
        bot.quiet_library_logs(lines.append)
        logging.getLogger("discord.http").debug("POST %s with %s has returned %s", "/x", {"content": "架空の本文"}, 200)
        logging.getLogger("discord.gateway").warning("Can't keep up")
        self.assertFalse(logging.getLogger("discord.http").isEnabledFor(logging.DEBUG))
        self.assertEqual(lines, ["discord.py WARNING: Can't keep up"])

    def test_errors_show_only_the_kind(self):
        lines = []
        bot.quiet_library_logs(lines.append)
        bot.quiet_library_logs(lines.append)   # 2回呼んでも二重に書かない
        try:
            raise ValueError("架空の本文")
        except ValueError:
            logging.getLogger("discord.client").exception("Ignoring exception")
        self.assertEqual(lines, ["discord.py ERROR: Ignoring exception（ValueError）"])


class RunForeverTest(unittest.TestCase):
    def run_with(self, *errors):
        """session が順に errors を投げる。errors が尽きたら止める。"""
        queue, lines, pauses = list(errors), [], []

        class Stop(Exception):
            pass

        async def session(token, build):
            raise queue.pop(0)

        def sleep(seconds):
            pauses.append(seconds)
            if not queue:
                raise Stop

        with self.assertRaises((Stop, bot.FatalGatewayError)) as caught:
            bot.run_forever("token", lambda: None, log=lines.append, session=session, sleep=sleep)
        return caught.exception, lines, pauses

    def test_wrong_token_stops_with_a_notice(self):
        error, _, pauses = self.run_with(discord.LoginFailure("bad"))
        self.assertIsInstance(error, bot.FatalGatewayError)
        self.assertEqual(pauses, [])

    def test_fatal_close_code_stops(self):
        closed = OSError("closed")
        closed.code = 4004
        error, _, _ = self.run_with(closed)
        self.assertIsInstance(error, bot.FatalGatewayError)

    def test_other_failures_retry_with_growing_pauses(self):
        error, lines, pauses = self.run_with(OSError("x"), OSError("x"), OSError("x"))
        self.assertNotIsInstance(error, bot.FatalGatewayError)
        self.assertEqual(pauses, [5.0, 10.0, 20.0])
        self.assertTrue(all("つなぎ直します" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
