"""通信しない部分のテスト。"""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import discord

from nepsync import rewrite, structure


def body(text, channels=None, messages=None):
    channels, messages = channels or {}, messages or {}
    return rewrite.rewrite_body(text, user_name={"1": "太郎"}.get, role_name={"2": "企画"}.get, channel_map=channels.get,
                                channel_name={"8": "外の部屋"}.get, message_map=messages.get, target_guild="900")


class RewriteTest(unittest.TestCase):
    def test_mentions_become_plain_names(self):
        self.assertEqual(body("<@1> と <@!1> と <@&2> へ"), "@太郎 と @太郎 と @企画 へ")
        self.assertEqual(body("<@77>"), "@不明なメンバー")

    def test_channel_mention_is_mapped_or_named(self):
        self.assertEqual(body("<#5>", {"5": "6"}), "<#6>")
        self.assertEqual(body("<#8>"), "#外の部屋")
        self.assertEqual(body("<#7>"), "<#7>")

    def test_links_to_paired_places_are_rewritten(self):
        self.assertEqual(body("https://discord.com/channels/100/5/9", {"5": "6"}, {"9": "19"}), "https://discord.com/channels/900/6/19")
        self.assertEqual(body("https://discord.com/channels/100/5/9", {"5": "6"}), "https://discord.com/channels/900/6")
        self.assertEqual(body("https://discord.com/channels/100/5", {"5": "6"}), "https://discord.com/channels/900/6")

    def test_other_links_are_left_alone(self):
        self.assertEqual(body("https://discord.com/channels/100/5/9"), "https://discord.com/channels/100/5/9")
        self.assertEqual(body("https://example.com/a"), "https://example.com/a")

    def test_compose_never_exceeds_the_limit(self):
        text = rewrite.compose(mark="印", quote="> 引用", body="あ" * 4000, notes=["添付"])
        self.assertLessEqual(len(text), 2000)
        self.assertIn("印", text)
        self.assertIn("添付", text)
        self.assertIn(rewrite.TRUNCATED, text)

    def test_delay_mark_only_when_late(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(rewrite.delay_mark("2026-11-09T11:55:00+00:00", now, 10), "")
        self.assertIn("遅れて同期", rewrite.delay_mark("2026-11-09T11:00:00+00:00", now, 10))

    def test_username_rules(self):
        self.assertNotIn("discord", rewrite.webhook_username("MyDiscordName").lower())
        self.assertEqual(rewrite.webhook_username(""), "メンバー")
        self.assertEqual(len(rewrite.webhook_username("あ" * 100)), 80)

    def test_name_comes_from_the_url(self):
        url = "https://cdn.discordapp.com/attachments/1/2/report_v2_final.pdf?ex=abc&is=def&hm=123"
        self.assertEqual(rewrite.attachment_filename(0, "pdf", False, url), "report_v2_final.pdf")

    def test_encoded_name_is_decoded(self):
        self.assertEqual(rewrite.attachment_filename(0, "png", False, "https://cdn.example/1/2/a%20b.png"), "a b.png")

    def test_spoiler_prefix_is_added_once(self):
        self.assertEqual(rewrite.attachment_filename(0, "png", True, "https://cdn.example/1/2/photo.png"), "SPOILER_photo.png")
        self.assertEqual(rewrite.attachment_filename(0, "png", True, "https://cdn.example/1/2/SPOILER_photo.png"), "SPOILER_photo.png")

    def test_name_cannot_break_the_upload(self):
        name = rewrite.attachment_filename(0, "png", False, 'https://cdn.example/1/2/a%22b%0d%0a%2f..%5cc.png')
        for bad in ('"', "\r", "\n", "/", "\\"):
            self.assertNotIn(bad, name)
        self.assertTrue(name.endswith(".png"))

    def test_extension_mismatch_or_no_url_falls_back(self):
        self.assertEqual(rewrite.attachment_filename(1, "png", False, "https://cdn.example/1/2/x.exe"), "file1.png")
        self.assertEqual(rewrite.attachment_filename(1, "png", False, None), "file1.png")
        self.assertEqual(rewrite.attachment_filename(1, "png", False, "https://cdn.example/1/2/"), "file1.png")

    def test_very_long_name_is_cut(self):
        self.assertLessEqual(len(rewrite.attachment_filename(0, None, False, "https://cdn.example/1/2/" + "a" * 500)), 200)

    def test_title_keeps_the_japanese_name(self):
        url = "https://cdn.discordapp.com/attachments/1/2/f17df61cc7237989.png?ex=1"
        self.assertEqual(rewrite.attachment_filename(0, "png", False, url, "議事録"), "議事録.png")
        self.assertEqual(rewrite.attachment_filename(0, "pdf", False, "https://cdn.example/1/2/3_.pdf", "第3回 議事録（確定）"), "第3回 議事録（確定）.pdf")

    def test_title_with_spoiler_is_not_prefixed_twice(self):
        self.assertEqual(rewrite.attachment_filename(0, "png", True, None, "SPOILER_議事録 2026"), "SPOILER_議事録 2026.png")
        self.assertEqual(rewrite.attachment_filename(0, "png", True, None, "議事録"), "SPOILER_議事録.png")

    def test_title_cannot_break_the_upload(self):
        name = rewrite.attachment_filename(0, "png", False, None, 'a"b\r\n/..\\c')
        for bad in ('"', "\r", "\n", "/", "\\"):
            self.assertNotIn(bad, name)
        self.assertTrue(name.endswith(".png"))

    def test_title_is_cut_but_keeps_its_extension(self):
        name = rewrite.attachment_filename(0, "pdf", False, None, "あ" * 500)
        self.assertLessEqual(len(name), 200)
        self.assertTrue(name.endswith(".pdf"))

    def test_empty_title_uses_the_url(self):
        self.assertEqual(rewrite.attachment_filename(0, "pdf", False, "https://cdn.example/1/2/a.pdf", ""), "a.pdf")
        self.assertEqual(rewrite.attachment_filename(0, "pdf", False, "https://cdn.example/1/2/a.pdf", None), "a.pdf")

    def test_neplector_filename_is_used(self):
        self.assertEqual(rewrite.attachment_filename(0, "pdf", False, "https://cdn.example/1/2/3_.pdf", None, "第3回 議事録.pdf"), "第3回 議事録.pdf")
        self.assertEqual(rewrite.attachment_filename(0, "pdf", False, None, None, "名前だけ"), "名前だけ.pdf")

    def test_title_wins_over_filename(self):
        self.assertEqual(rewrite.attachment_filename(0, "png", False, None, "議事録", "2026.png"), "議事録.png")

    def test_old_attachment_without_any_name(self):
        self.assertEqual(rewrite.attachment_filename(3, "mp4", False, None, None, None), "file3.mp4")
        self.assertEqual(rewrite.attachment_filename(3, None, True, None, None, None), "SPOILER_file3")

    def test_spoiler_filename(self):
        self.assertEqual(rewrite.attachment_filename(0, "png", True), "SPOILER_file0.png")
        self.assertEqual(rewrite.attachment_filename(1, ".jpg", False), "file1.jpg")
        self.assertEqual(rewrite.attachment_filename(2, None, False), "file2")


def channel(id_, type_, name, position=0, **extra):
    """discord.py のチャンネルと同じ名前の値を持つ、架空のチャンネル。"""
    values = {"topic": None, "slowmode_delay": 0, "nsfw": False, "available_tags": [], **extra}
    return SimpleNamespace(id=int(id_), type=discord.ChannelType(type_), name=name, position=position, **values)


def text(id_, name, position=0, **extra):
    return channel(id_, 0, name, position, **extra)


def forum(id_, name, position=0, **extra):
    return channel(id_, 15, name, position, **extra)


class MergeTest(unittest.TestCase):
    def merge(self, a, b, snapshot, flow_b=True):
        return structure.merge({"name": a}, {"name": b}, None if snapshot is None else {"name": snapshot}, flow_a=True, flow_b=flow_b, priority="a")

    def test_only_the_changed_side_is_copied(self):
        self.assertEqual(self.merge("新", "旧", "旧").write_b, {"name": "新"})
        self.assertEqual(self.merge("旧", "新", "旧").write_a, {"name": "新"})

    def test_same_values_write_nothing(self):
        change = self.merge("同", "同", "旧")
        self.assertEqual((change.write_a, change.write_b), ({}, {}))
        self.assertEqual(change.snapshot, {"name": "同"})

    def test_both_changed_uses_priority_and_reports(self):
        change = self.merge("Aの", "Bの", "旧")
        self.assertEqual(change.write_b, {"name": "Aの"})
        self.assertEqual(change.write_a, {})
        self.assertEqual(change.conflicts, ["name"])

    def test_one_way_never_writes_to_the_source(self):
        change = self.merge("旧", "勝手に変更", "旧", flow_b=False)
        self.assertEqual(change.write_a, {})
        self.assertEqual(change.write_b, {"name": "旧"})

    def test_empty_values_are_treated_the_same(self):
        self.assertEqual(structure.fields_of(text("1", "a", topic=None)), structure.fields_of(text("2", "a", topic="")))


class PlanTest(unittest.TestCase):
    def plan(self, a, b, pairs=(), readable=None, flow_b=False):
        readable = {str(item.id) for item in a + b} if readable is None else readable
        return structure.plan(a, b, list(pairs), readable, flow_a=True, flow_b=flow_b, priority="a")

    def test_same_name_different_kind_are_separate(self):
        result = self.plan([text("1", "test"), forum("2", "test")], [text("3", "test"), forum("4", "test")])
        self.assertCountEqual(result.new_pairs, [("1", "3"), ("2", "4")])
        self.assertEqual(result.creates, [])

    def test_text_is_not_paired_with_a_forum_of_the_same_name(self):
        result = self.plan([text("1", "test")], [forum("4", "test")])
        self.assertEqual(result.new_pairs, [])
        self.assertEqual([item[1].id for item in result.creates], [1])

    def test_duplicates_are_reported_not_guessed(self):
        result = self.plan([text("1", "x"), text("2", "x")], [text("3", "x")])
        self.assertEqual(result.new_pairs, [])
        self.assertEqual(result.creates, [])
        self.assertTrue(result.notices)

    def test_unread_channel_is_not_copied(self):
        result = self.plan([text("1", "運営だけ")], [], readable=set())
        self.assertEqual(result.creates, [])

    def test_one_way_ignores_new_channels_on_the_target(self):
        result = self.plan([], [text("3", "相手の新しい部屋")])
        self.assertEqual(result.creates, [])

    def test_missing_is_reported_but_never_a_delete(self):
        pairs = [{"a_id": "1", "b_id": "3", "snapshot": None}]
        result = self.plan([], [text("3", "x")], pairs)
        self.assertEqual(result.missing, [("a", "1")])

    def test_other_channel_types_are_not_synced(self):
        result = self.plan([channel("1", 5, "お知らせ")], [])
        self.assertEqual(result.creates, [])
        self.assertTrue(result.notices)


class OrderTest(unittest.TestCase):
    pairs = [{"a_id": "1", "b_id": "3"}, {"a_id": "2", "b_id": "4"}]

    def test_same_order_does_nothing(self):
        side, moves, order = structure.reorder_plan([text("1", "x", 1), text("2", "y", 2)], [text("3", "x", 5), text("4", "y", 6)], self.pairs, None, flow_a=True, flow_b=False, priority="a")
        self.assertIsNone(side)
        self.assertEqual(order, ["1", "2"])

    def test_source_order_is_copied(self):
        side, goal, _ = structure.reorder_plan([text("1", "x", 2), text("2", "y", 1)], [text("3", "x", 5), text("4", "y", 6)], self.pairs, ["1", "2"], flow_a=True, flow_b=False, priority="a")
        self.assertEqual(side, "b")
        self.assertEqual(goal, ["4", "3"])

    def test_both_way_follows_the_side_that_changed(self):
        side, goal, _ = structure.reorder_plan([text("1", "x", 1), text("2", "y", 2)], [text("3", "x", 6), text("4", "y", 5)], self.pairs, ["1", "2"], flow_a=True, flow_b=True, priority="a")
        self.assertEqual(side, "a")
        self.assertEqual(goal, ["2", "1"])

    def test_next_move_puts_one_channel_in_place_at_a_time(self):
        self.assertEqual(structure.next_move(["1", "2", "3"], ["1", "2", "3"]), None)
        self.assertEqual(structure.next_move(["1", "2", "3"], ["3", "1", "2"]), ("3", "1"))
        self.assertEqual(structure.next_move(["3", "2", "1"], ["3", "1", "2"]), ("1", "2"))


def overwrite(target, allow=0, deny=0):
    return discord.Object(id=int(target)), discord.PermissionOverwrite.from_pair(discord.Permissions(allow), discord.Permissions(deny))


class EffectivePermissionsTest(unittest.TestCase):
    roles = [SimpleNamespace(id=1, permissions=discord.Permissions(1 << 10)), SimpleNamespace(id=2, permissions=discord.Permissions(0)),
             SimpleNamespace(id=3, permissions=discord.Permissions(1 << 3))]

    def compute(self, user_roles, *overwrites):
        return structure.effective_permissions("1", "9", user_roles, self.roles, dict(overwrites))

    def test_everyone_override_counts(self):
        self.assertTrue(self.compute([], overwrite("1", allow=1 << 4)) & (1 << 4))

    def test_member_override_beats_everyone_deny(self):
        self.assertTrue(self.compute([], overwrite("1", deny=1 << 10), overwrite("9", allow=1 << 10)) & (1 << 10))

    def test_everyone_deny_hides(self):
        self.assertFalse(self.compute([], overwrite("1", deny=1 << 10)) & (1 << 10))

    def test_role_override_counts(self):
        self.assertTrue(self.compute(["2"], overwrite("2", allow=1 << 29)) & (1 << 29))

    def test_administrator_has_everything(self):
        self.assertTrue(self.compute(["3"], overwrite("1", deny=1 << 10)) & (1 << 10))


if __name__ == "__main__":
    unittest.main()
