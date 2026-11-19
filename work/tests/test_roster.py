"""名簿（/nepsync-members）と「この人を呼ぶ」のテスト（ADR 0014）。データはすべて架空。"""

import unittest

from nepsync import grid
from nepsync.commands import Commands
from nepsync.neplector import Transient
from nepsync.roster import arrange, fold, matches

from .fakes import GUILD_A, GUILD_B, NOW, copy_of, started

ASKER = "900"   # A にいる、打った人


def person(user_id, name, *names):
    return {"user_id": user_id, "name": name, "names": [name, *names], "avatar": None, "can_send": True}


class FoldAndMatchTest(unittest.TestCase):
    def test_width_case_kana_and_spaces_are_ignored(self):
        self.assertEqual(fold("ＴＡＲＯ"), fold("taro"))
        self.assertEqual(fold("タロウ"), fold("たろう"))
        self.assertEqual(fold("ﾀﾛｳ"), fold("たろう"))
        self.assertEqual(fold("た ろ う"), fold("たろう"))

    def test_front_matches_come_first_then_inside(self):
        people = [person("1", "まるたろう"), person("2", "たろう"), person("3", "はなこ")]
        self.assertEqual([item["user_id"] for item in matches(people, "タロ")], ["2", "1"])

    def test_other_names_are_searched_too(self):
        people = [person("1", "B での名前", "Taro")]
        self.assertEqual(len(matches(people, "taro")), 1)

    def test_empty_query_keeps_everyone(self):
        people = [person("1", "あ"), person("2", "い")]
        self.assertEqual(matches(people, " "), people)


class ArrangeTest(unittest.TestCase):
    def test_star_then_picked_then_recent_then_name(self):
        people = [person(str(number), name) for number, name in enumerate(["え", "う", "い", "あ", "お"])]
        out = arrange(people, stars=["4"], picks=["2", "4"], recent=["1", "2"])
        self.assertEqual([item["user_id"] for item in out], ["4", "2", "1", "3", "0"])
        self.assertEqual([item["mark"] for item in out], ["star", "picked", "recent", "", ""])

    def test_people_not_in_the_list_are_ignored(self):
        out = arrange([person("1", "あ")], stars=["99"], picks=[], recent=[])
        self.assertEqual(out[0]["mark"], "")


class GridTest(unittest.TestCase):
    def test_png_has_room_for_every_cell(self):
        cells = [{"number": index + 1, "name": f"架空{index}", "mark": ["star", "picked", "recent", ""][index % 4], "avatar": None}
                 for index in range(7)]
        data = grid.render(cells)
        self.assertTrue(data.startswith(b"\x89PNG"))
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.size, (grid.COLUMNS * grid.CELL_W + grid.PAD * 2, 2 * grid.CELL_H + grid.PAD * 2))

    def test_broken_avatar_falls_back(self):
        self.assertTrue(grid.render([{"number": 1, "name": "架空", "mark": "", "avatar": b"not a picture"}]).startswith(b"\x89PNG"))

    def test_long_names_are_cut(self):
        face = grid.font(19)
        self.assertTrue(grid.fit("とても" * 30, face, 100).endswith("…"))
        self.assertEqual(grid.fit("短い", face, 100), "短い")


class RosterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, self.notices = await started("both")
        self.commands = Commands(self.engine.config, self.nep, self.discord.client, self.engine.store, log=self.notices.append, clock=NOW.timestamp)
        self.cells: list[list[dict]] = []
        self.commands.roster.render = lambda cells: self.cells.append(cells) or b"png"
        self.nep.tables["member_servers"].append({"guild_id": GUILD_A, "user_id": ASKER, "display_name": "打った人", "avatar_url": None})
        self.copy_channel = copy_of(self.engine, "111")

    def add_b(self, user_id, name, avatar=None):
        self.nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": user_id, "display_name": name, "avatar_url": avatar})

    async def page(self, page=0, guild=GUILD_A, channel="111", user=ASKER, query=None, selected=None):
        return await self.commands.members(channel, page, guild, user, query, selected)

    async def names(self, **where):
        body = await self.page(**where)
        return [cell["name"] for cell in self.cells[-1]] if body["kind"] == "page" else []

    # 判断1：相手のサーバーにだけいる人
    async def test_only_people_only_on_the_other_server_are_listed(self):
        self.add_b("501", "Bでの架空さん")   # 501 は A にもいる
        self.assertEqual(await self.names(), ["相手さん"])

    async def test_from_the_other_side_too(self):
        self.assertEqual(await self.names(guild=GUILD_B, channel=self.copy_channel, user="502"), ["打った人", "架空さん"])

    async def test_hidden_and_bots_are_not_listed(self):
        self.add_b("602", "bot")
        self.add_b("603", "隠した人")
        self.nep.tables["members"] = [{"user_id": "602", "is_bot": 1}]
        self.engine.store.set_hidden("603", True)
        self.assertEqual(await self.names(), ["相手さん"])

    async def test_people_who_cannot_see_the_channel_are_not_listed(self):
        self.discord.roles[GUILD_B] = [{"id": GUILD_B, "permissions": "0"}]
        self.assertEqual((await self.page())["kind"], "text")

    async def test_role_gives_access_and_view_only_is_told_apart(self):
        self.discord.roles[GUILD_B] = [{"id": GUILD_B, "permissions": "0"}, {"id": "70", "permissions": str(1 << 10)}]
        self.nep.tables["role_periods"] = [{"user_id": "502", "role_id": "70", "started_at": "x", "ended_at": None}]
        self.assertEqual((await self.page())["options"][0]["description"], "見るだけ")

    async def test_owner_can_always_see(self):
        self.discord.roles[GUILD_B] = [{"id": GUILD_B, "permissions": "0"}]
        self.discord.owners[GUILD_B] = "502"
        self.assertEqual(await self.names(), ["相手さん"])

    async def test_unsynced_channel_is_refused(self):
        body = await self.page(channel="404")
        self.assertEqual(body["kind"], "text")
        self.assertIn("同期しているチャンネル", body["content"])

    async def test_nobody_to_show(self):
        self.nep.tables["member_servers"] = [row for row in self.nep.tables["member_servers"] if row["guild_id"] != GUILD_B]
        body = await self.page()
        self.assertEqual(body["kind"], "text")
        self.assertIn("いません", body["content"])

    # 判断2：格子とメニュー
    async def test_page_has_image_menu_and_numbers(self):
        body = await self.page()
        self.assertEqual(body["kind"], "page")
        self.assertEqual(body["image"], b"png")
        self.assertEqual(body["options"], [{"label": "1. 相手さん", "value": "502", "description": "発言できる"}])
        self.assertIsNone(body["pager"])
        self.assertEqual(self.cells[-1][0]["number"], 1)
        self.assertEqual(body["content"], "")
        self.assertEqual((body["selected"], body["star"]), ([], None))

    async def test_page_keeps_what_was_chosen(self):
        self.engine.store.set_star(ASKER, "502", True, 1.0)
        body = await self.page(selected=["502", "x"])
        self.assertEqual(body["content"], "<@502>")
        self.assertEqual(body["embeds"], [{"description": "```\n<@502>\n```"}])
        self.assertEqual((body["selected"], body["star"]), (["502"], True))

    async def test_pages_of_25(self):
        for number in range(30):
            self.add_b(f"70{number:02d}", f"人{number:02d}")
        body = await self.page()
        self.assertEqual(len(body["options"]), 25)
        self.assertEqual(body["pager"], {"channel_id": "111", "page": 0, "pages": 2})
        body = await self.page(page=1)
        self.assertEqual(len(body["options"]), 6)
        self.assertEqual(body["options"][0]["label"].split(".")[0], "26")
        self.assertEqual((await self.page(page=9))["pager"]["page"], 1)
        self.assertEqual((await self.page(page=-1))["pager"]["page"], 0)

    async def test_avatars_are_fetched_small_and_kept(self):
        fetched = []

        async def download(url):
            fetched.append(url)
            return b"picture"
        self.commands.roster.download = download
        self.add_b("701", "アイコンの人", "https://cdn.example.invalid/avatars/701/abc.webp")
        await self.page()
        await self.page()
        self.assertEqual(fetched, ["https://cdn.example.invalid/avatars/701/abc.png?size=128"])
        self.assertEqual([cell["avatar"] for cell in self.cells[-1]], [b"picture", None])

    async def test_avatar_failure_still_shows_the_page(self):
        async def download(url):
            raise Transient("x")
        self.commands.roster.download = download
        self.add_b("701", "アイコンの人", "https://cdn.example.invalid/a.png")
        self.assertEqual((await self.page())["kind"], "page")

    async def test_failure_gives_a_message_without_details(self):
        async def refused(name, **filters):
            raise Transient("x")
        self.nep.table = refused
        body = await self.page()
        self.assertEqual(body["kind"], "text")
        self.assertIn("作れませんでした", body["content"])
        self.assertEqual(self.notices, ["名簿を作れませんでした: Transient"])

    # 判断3：並べる順
    async def test_order_star_picked_recent(self):
        for user_id, name in (("701", "あ"), ("702", "い"), ("703", "う")):
            self.add_b(user_id, name)
        self.engine.store.set_star(ASKER, "703", True, 1.0)
        self.engine.store.add_picks(ASKER, ["702"], 1.0, 8)
        names = await self.names()
        self.assertEqual(names[:2], ["う", "い"])
        self.assertEqual([cell["mark"] for cell in self.cells[-1]][:2], ["star", "picked"])

    async def test_recent_speakers_come_from_the_copies_in_this_channel(self):
        for user_id, name in (("701", "あ"), ("702", "い")):
            self.add_b(user_id, name)
        self.nep.say("880001", self.copy_channel, GUILD_B, "架空の発言", author="702")
        await self.engine.handle_batch([self.nep.rows[-1]])
        self.assertEqual((await self.names())[0], "い")
        self.assertEqual(self.cells[-1][0]["mark"], "recent")

    async def test_stars_are_per_person(self):
        self.add_b("701", "あ")
        self.engine.store.set_star("999", "502", True, 1.0)
        await self.page()
        self.assertEqual([cell["mark"] for cell in self.cells[-1]], ["", ""])

    # 判断4：探す
    async def test_search_one_hit_goes_straight_to_pick(self):
        self.add_b("701", "はなこ")
        body = await self.page(query="ハナ")
        self.assertEqual(body["kind"], "pick")
        self.assertEqual(body["content"], "<@701>")

    async def test_search_by_chosen_id(self):
        self.add_b("701", "相手さん二号")
        body = await self.page(query="701")
        self.assertEqual(body["targets"], ["701"])

    async def test_search_many_hits_shows_a_grid_without_pages(self):
        self.add_b("701", "相手さん二号")
        body = await self.page(query="相手")
        self.assertEqual(body["kind"], "page")
        self.assertEqual(len(body["options"]), 2)
        self.assertIsNone(body["pager"])
        self.assertEqual(body["embeds"], [])

    async def test_search_no_hit(self):
        body = await self.page(query="だれでもない")
        self.assertEqual(body["kind"], "text")
        self.assertIn("当たる人", body["content"])

    async def test_suggest_lists_marks_and_ids(self):
        self.add_b("701", "はなこ")
        self.engine.store.set_star(ASKER, "701", True, 1.0)
        found = await self.commands.suggest("111", GUILD_A, ASKER, "")
        self.assertEqual(found, [("★ はなこ", "701"), ("相手さん", "502")])
        self.assertEqual(await self.commands.suggest("111", GUILD_A, ASKER, "あいて"), [])
        self.assertEqual(await self.commands.suggest("111", GUILD_A, ASKER, "相手"), [("相手さん", "502")])

    async def test_suggest_failure_is_empty(self):
        async def refused(name, **filters):
            raise Transient("x")
        self.nep.table = refused
        self.assertEqual(await self.commands.suggest("111", GUILD_A, ASKER, "a"), [])

    # 判断4・5：選んだあと
    async def test_pick_gives_bare_mentions_and_remembers(self):
        self.add_b("701", "はなこ")
        body = await self.commands.pick(GUILD_A, ASKER, ["502", "701", "502"])
        self.assertEqual(body["content"], "<@502> <@701>")
        self.assertEqual(body["embeds"][0]["title"], "相手さん・はなこ")
        self.assertEqual(body["embeds"][0]["description"], "```\n<@502> <@701>\n```")
        self.assertIsNone(body["star"])
        self.assertEqual(set(self.engine.store.picks(ASKER)), {"502", "701"})

    async def test_pick_one_shows_star_state_and_icon(self):
        self.add_b("701", "はなこ", "https://example.invalid/h.png")
        self.engine.store.set_star(ASKER, "701", True, 1.0)
        body = await self.commands.pick(GUILD_A, ASKER, ["701"])
        self.assertTrue(body["star"])
        self.assertEqual(body["embeds"][0]["thumbnail"], {"url": "https://example.invalid/h.png"})

    async def test_pick_rejects_odd_values_and_caps_at_ten(self):
        body = await self.commands.pick(GUILD_A, ASKER, ["x", ""])
        self.assertEqual(body["kind"], "text")
        body = await self.commands.pick(GUILD_A, ASKER, [str(800 + number) for number in range(15)])
        self.assertEqual(len(body["targets"]), 10)

    async def test_picks_keep_the_newest_eight(self):
        for number in range(12):
            self.engine.store.add_picks(ASKER, [str(800 + number)], float(number), 8)
        self.assertEqual(self.engine.store.picks(ASKER), [str(800 + number) for number in range(11, 3, -1)])

    async def test_star_toggle(self):
        self.assertTrue(self.commands.roster.set_star(ASKER, "502", True))
        self.assertEqual(self.engine.store.stars(ASKER), ["502"])
        self.assertTrue(self.commands.roster.set_star(ASKER, "502", False))
        self.assertEqual(self.engine.store.stars(ASKER), [])
        self.assertFalse(self.commands.roster.set_star(ASKER, "abc", True))

    async def test_only_ids_are_stored(self):
        await self.commands.pick(GUILD_A, ASKER, ["502"])
        self.commands.roster.set_star(ASKER, "502", True)
        dump = "\n".join(self.engine.store.db.iterdump())
        self.assertIn("502", dump)
        self.assertNotIn("相手さん", dump)

    async def test_leaving_neplector_forgets_stars_and_picks_both_ways(self):
        self.engine.store.set_star(ASKER, "502", True, 1.0)
        self.engine.store.add_picks("502", [ASKER], 1.0, 8)
        self.nep.forget_rows = [{"seq": 1, "kind": "user", "id": "502"}]
        await self.engine.process_forget()
        self.assertEqual(self.engine.store.stars(ASKER), [])
        self.assertEqual(self.engine.store.picks("502"), [])

    # 判断6：この人を呼ぶ
    async def test_call_author_of_a_copy(self):
        self.nep.say("880002", self.copy_channel, GUILD_B, "架空の発言", author="502")
        await self.engine.handle_batch([self.nep.rows[-1]])
        copy_id = self.engine.store.message_by_source("880002")["copy_id"]
        body = await self.commands.call_author(copy_id, GUILD_A, ASKER)
        self.assertEqual(body["content"], "<@502>")
        self.assertIn("502", self.engine.store.picks(ASKER))

    async def test_call_author_works_for_hidden_people(self):
        self.engine.store.set_hidden("502", True)
        self.nep.say("880003", self.copy_channel, GUILD_B, "架空の発言", author="502")
        await self.engine.handle_batch([self.nep.rows[-1]])
        copy_id = self.engine.store.message_by_source("880003")["copy_id"]
        self.assertEqual((await self.commands.call_author(copy_id, GUILD_A, ASKER))["content"], "<@502>")

    async def test_call_author_on_a_non_copy(self):
        body = await self.commands.call_author("123456", GUILD_A, ASKER)
        self.assertEqual(body["kind"], "text")
        self.assertIn("写しにだけ", body["content"])


class ReplyPingTest(unittest.IsolatedAsyncioTestCase):
    """判断7：写しに返信すると、元の人に通知する。"""

    async def asyncSetUp(self):
        self.engine, self.nep, self.discord, _ = await started("both")
        self.copy_channel = copy_of(self.engine, "111")
        self.nep.say("880010", self.copy_channel, GUILD_B, "B の架空の発言", author="502")
        await self.engine.handle_batch([self.nep.rows[-1]])
        self.copy_id = self.engine.store.message_by_source("880010")["copy_id"]

    async def reply(self, author="501", **extra):
        self.nep.say("880011", "111", GUILD_A, "返事", author=author, reply_to_id=self.copy_id, **extra)
        await self.engine.handle_batch([self.nep.rows[-1]])
        return self.discord.messages[self.engine.store.message_by_source("880011")["copy_id"]]

    async def test_reply_to_a_copy_notifies_the_original_author(self):
        sent = await self.reply()
        self.assertIn("<@502>", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "users": ["502"]})

    async def test_reply_to_a_copy_of_my_own_message_does_not_notify(self):
        sent = await self.reply(author="502")
        self.assertNotIn("<@502>", sent["content"])
        self.assertEqual(sent["allowed_mentions"], {"parse": []})

    async def test_editing_the_reply_does_not_notify_again(self):
        await self.reply()
        pair = self.engine.store.message_by_source("880011")
        self.nep.bodies["880011"].update(content="返事（直した）", edited_at="2026-11-09T12:00:00+00:00")
        self.nep.event("message.update", message_id="880011", channel_id="111")
        await self.engine.handle_batch([self.nep.rows[-1]])
        edited = self.discord.messages[pair["copy_id"]]
        self.assertIn("<@502>", edited["content"])
        self.assertEqual(edited["allowed_mentions"], {"parse": []})
