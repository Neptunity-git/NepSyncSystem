"""同期の本体のテスト。Neplector と Discord は偽物で、データは架空。"""

import unittest
from datetime import timedelta

import discord as discord_errors

from nepsync.neplector import NeplectorError, Transient

from .fakes import CAT_A, CAT_B, GUILD_A, GUILD_B, NOW, copy_of, forbidden, make_world, run, started


def posted(discord):
    return list(discord.messages.values())


class StructureTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_is_created_on_the_other_side_with_its_settings(self):
        engine, nep, discord, _ = make_world()
        await engine.sync_structure()
        copy = discord.channels[copy_of(engine, "111")]
        self.assertEqual((copy["name"], copy["type"], copy["parent_id"], copy["guild_id"]), ("雑談", 0, CAT_B, GUILD_B))
        self.assertEqual(copy["topic"], "架空のトピック")

    async def test_second_pass_changes_nothing(self):
        engine, nep, discord, _ = make_world()
        await engine.sync_structure()
        await engine.sync_structure()
        discord.writes.clear()
        await engine.sync_structure()
        self.assertEqual(discord.writes, [])

    async def test_rename_and_topic_flow(self):
        engine, nep, discord, _ = await started()
        await engine.sync_structure()
        discord.channels["111"].update(name="改名", topic="新しいトピック")
        await engine.sync_structure()
        copy = discord.channels[copy_of(engine, "111")]
        self.assertEqual((copy["name"], copy["topic"]), ("改名", "新しいトピック"))

    async def test_one_way_never_writes_to_the_source_server(self):
        engine, nep, discord, _ = await started()
        await engine.sync_structure()
        copy = copy_of(engine, "111")
        discord.channels[copy].update(name="相手で勝手に変更")
        discord.add("211", GUILD_B, 0, "相手の新しい部屋", CAT_B)
        discord.channels[CAT_B]["name"] = "相手で改名"
        discord.channels[copy]["position"] = 99
        discord.writes.clear()
        await engine.sync_structure()
        self.assertFalse([item for item in discord.writes if GUILD_A in item], discord.writes)
        self.assertEqual(discord.channels["111"]["name"], "雑談")
        self.assertEqual(discord.channels[CAT_A]["name"], "共同")
        self.assertEqual(discord.channels[copy]["name"], "雑談")   # 写しは元に戻る

    async def test_both_ways_a_change_on_b_reaches_a(self):
        engine, nep, discord, _ = await started("both")
        await engine.sync_structure()
        copy = copy_of(engine, "111")
        discord.channels[copy]["name"] = "Bで改名"
        await engine.sync_structure()
        self.assertEqual(discord.channels["111"]["name"], "Bで改名")
        discord.writes.clear()
        await engine.sync_structure()
        self.assertEqual(discord.writes, [])   # 輪にならない

    async def test_private_channel_in_the_category_is_not_copied(self):
        engine, nep, discord, _ = make_world()
        discord.add("112", GUILD_A, 0, "運営だけ", CAT_A)   # Neplector が読んでいない
        await engine.sync_structure()
        self.assertIsNone(engine.store.channel_side("112"))
        self.assertFalse([item for item in discord.channels.values() if item["name"] == "運営だけ" and item["guild_id"] == GUILD_B])

    async def test_copy_deleted_by_hand_is_recreated_in_one_way(self):
        engine, nep, discord, notices = await started()
        old = copy_of(engine, "111")
        del discord.channels[old]
        await engine.sync_structure()
        await engine.sync_structure()
        self.assertNotEqual(copy_of(engine, "111"), old)
        self.assertTrue(notices)

    async def test_dry_run_changes_nothing(self):
        engine, nep, discord, _ = make_world()
        actions = await engine.sync_structure(dry_run=True)
        self.assertTrue(any("作る" in item for item in actions))
        self.assertEqual(discord.writes, [])
        self.assertEqual(engine.store.channel_pairs(), [])


async def three_rooms(direction):
    """A に 雑談・二つ目・三つ目 の順で3つ。B に写しが同じ順である世界。"""
    engine, nep, discord, notices = make_world(direction)
    for channel_id, name in (("112", "二つ目"), ("113", "三つ目")):
        discord.add(channel_id, GUILD_A, 0, name, CAT_A)
        nep.tables["channels"].append({"channel_id": channel_id, "category_id": CAT_A, "kind": "text", "name": name, "reading": 1, "guild_id": GUILD_A})
    await engine.sync_structure()
    await engine.sync_structure()
    discord.writes.clear()
    return engine, nep, discord, notices


def order(discord, category):
    rooms = [item for item in discord.channels.values() if item.get("parent_id") == category and item["type"] == 0]
    return [item["name"] for item in sorted(rooms, key=lambda item: (item["position"], int(item["id"])))]


class OrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_order_is_copied_one_move_at_a_time(self):
        engine, nep, discord, _ = await three_rooms("a_to_b")
        self.assertEqual(order(discord, CAT_B), ["雑談", "二つ目", "三つ目"])
        discord.channels["113"]["position"] = 1
        await engine.sync_structure()
        self.assertEqual(order(discord, CAT_B), ["三つ目", "雑談", "二つ目"])
        self.assertFalse([item for item in discord.writes if item[1] == GUILD_A])
        self.assertIsNone(engine.store.get(f"order_goal:{CAT_A}"))
        discord.writes.clear()
        await engine.sync_structure()
        self.assertEqual(discord.writes, [])

    async def test_stopped_halfway_continues_instead_of_copying_back(self):
        """両方向で、並べ替えの途中で止まっても、動かしかけの B の並びを A に写し返さない。"""
        engine, nep, discord, _ = await three_rooms("both")
        for channel_id, position in (("111", 30), ("112", 20), ("113", 10)):
            discord.channels[channel_id]["position"] = position
        discord.fail("move", None, forbidden(50013, "Missing Permissions"))
        with self.assertRaises(discord_errors.Forbidden):
            await engine.sync_structure()
        self.assertNotEqual(order(discord, CAT_B), ["三つ目", "二つ目", "雑談"])
        await engine.sync_structure()
        self.assertEqual(order(discord, CAT_A), ["三つ目", "二つ目", "雑談"])
        self.assertEqual(order(discord, CAT_B), ["三つ目", "二つ目", "雑談"])
        self.assertFalse([item for item in discord.writes if item[1] == GUILD_A])

    async def test_waits_for_the_notice_before_the_next_move(self):
        """並べ替えの知らせが届かないうちは、次の1手を古い控えで計算しない。目標は残して次の回に続ける。"""
        from nepsync import engine as engine_module
        engine, nep, discord, notices = await three_rooms("a_to_b")
        discord.channels["113"]["position"] = 1
        discord.lagging = True
        saved, engine_module.SETTLE_SECONDS = engine_module.SETTLE_SECONDS, 0
        try:
            await engine._keep_going("構造の見回り", engine.sync_structure)
        finally:
            engine_module.SETTLE_SECONDS = saved
        self.assertEqual(len([item for item in discord.writes if item[0] == "reorder"]), 1)
        self.assertIsNotNone(engine.store.get(f"order_goal:{CAT_A}"))
        self.assertEqual(notices, [])
        discord.lagging = False
        await engine.sync_structure()
        self.assertEqual(order(discord, CAT_B), ["三つ目", "雑談", "二つ目"])


class ChannelDeleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_never_deletes(self):
        engine, nep, discord, notices = await started()
        copy = copy_of(engine, "111")
        nep.event("channel.stop", channel_id="111")
        await engine.handle_row(nep.rows[-1], None)
        self.assertIn(copy, discord.channels)
        self.assertTrue(notices)

    async def test_real_delete_removes_the_copy(self):
        engine, nep, discord, notices = await started()
        copy = copy_of(engine, "111")
        del discord.channels["111"]
        nep.event("channel.delete", channel_id="111")
        await engine.handle_row(nep.rows[-1], None)
        self.assertNotIn(copy, discord.channels)
        self.assertIsNone(engine.store.channel_side("111"))

    async def test_delete_event_while_channel_still_exists_does_not_delete(self):
        engine, nep, discord, notices = await started()
        copy = copy_of(engine, "111")
        nep.event("channel.delete", channel_id="111")
        await engine.handle_row(nep.rows[-1], None)
        self.assertIn(copy, discord.channels)
        self.assertTrue(notices)

    async def test_forbidden_is_not_treated_as_gone(self):
        engine, nep, discord, notices = await started()
        copy = copy_of(engine, "111")
        discord.forbidden.add("111")
        nep.event("channel.delete", channel_id="111")
        await engine.handle_row(nep.rows[-1], None)
        self.assertIn(copy, discord.channels)

    async def test_limit_holds_further_deletes_until_approved(self):
        engine, nep, discord, notices = make_world(delete_limit_per_hour=1)
        discord.add("112", GUILD_A, 0, "二つ目", CAT_A)
        nep.tables["channels"].append({"channel_id": "112", "category_id": CAT_A, "kind": "text", "name": "二つ目", "reading": 1, "guild_id": GUILD_A})
        await engine.sync_structure()
        first, second = copy_of(engine, "111"), copy_of(engine, "112")
        for channel_id in ("111", "112"):
            del discord.channels[channel_id]
            nep.event("channel.delete", channel_id=channel_id)
            await engine.handle_row(nep.rows[-1], None)
        self.assertNotIn(first, discord.channels)
        self.assertIn(second, discord.channels)
        self.assertEqual(engine.store.held_deletes(), ["112"])
        self.assertEqual(await engine.approve_held_deletes(), 1)
        self.assertNotIn(second, discord.channels)

    async def test_delete_can_be_switched_off(self):
        engine, nep, discord, notices = make_world(allow_delete=False)
        await engine.sync_structure()
        copy = copy_of(engine, "111")
        del discord.channels["111"]
        nep.event("channel.delete", channel_id="111")
        await engine.handle_row(nep.rows[-1], None)
        self.assertIn(copy, discord.channels)

    async def test_deleting_the_copy_in_one_way_does_not_touch_the_source(self):
        engine, nep, discord, _ = await started()
        copy = copy_of(engine, "111")
        nep.event("channel.delete", channel_id=copy)
        await engine.handle_row(nep.rows[-1], None)
        self.assertIn("111", discord.channels)

    async def test_own_delete_notice_in_both_ways_stops(self):
        engine, nep, discord, _ = await started("both")
        copy = copy_of(engine, "111")
        del discord.channels["111"]
        nep.event("channel.delete", channel_id="111")
        await engine.handle_row(nep.rows[-1], None)
        discord.writes.clear()
        nep.event("channel.delete", channel_id=copy)   # 自分が消した写しの知らせ
        await engine.handle_row(nep.rows[-1], None)
        self.assertEqual(discord.writes, [])


class MessageTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_is_copied_with_name_and_icon_and_no_pings(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "@everyone こんにちは")
        await run(engine)
        sent = posted(discord)[0]
        self.assertEqual(sent["username"], "架空さん")
        self.assertEqual(sent["avatar_url"], "https://example.invalid/a.png")
        self.assertEqual(sent["allowed_mentions"], {"parse": []})
        self.assertEqual(sent["channel_id"], copy_of(engine, "111"))
        self.assertIn("こんにちは", sent["content"])

    async def test_body_is_never_stored(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "とても秘密の架空の本文")
        await run(engine)
        dump = "\n".join(engine.store.db.iterdump())
        self.assertNotIn("とても秘密の架空の本文", dump)

    async def test_same_message_is_not_copied_twice(self):
        engine, nep, discord, _ = await started()
        row = nep.say("1", "111", GUILD_A)
        await run(engine)
        await engine.handle_batch([row])
        self.assertEqual(len(posted(discord)), 1)

    async def test_no_loop_from_webhook_or_bot_messages(self):
        engine, nep, discord, _ = await started("both")
        target = copy_of(engine, "111")
        nep.say("1", "111", GUILD_A)
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        count = len(posted(discord))
        engine.store.set("changes_after", nep.last_seq())
        nep.say(copy_id, target, GUILD_B, "写し", author="999", counted=0, webhook_id="7")  # counted = 0
        nep.say("2", target, GUILD_B, "別の Webhook", counted=1, webhook_id="7")            # 念のための二重の確認
        await run(engine)
        self.assertEqual(len(posted(discord)), count)
        self.assertNotIn(copy_id, nep.requested)

    async def test_copy_id_seen_as_counted_is_still_not_reposted(self):
        engine, nep, discord, _ = await started("both")
        target = copy_of(engine, "111")
        nep.say("1", "111", GUILD_A)
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        count = len(posted(discord))
        engine.store.set("changes_after", nep.last_seq())
        nep.say(copy_id, target, GUILD_B, "写し", counted=1)
        await run(engine)
        self.assertEqual(len(posted(discord)), count)

    async def test_one_way_ignores_messages_on_the_target(self):
        engine, nep, discord, _ = await started()
        nep.say("1", copy_of(engine, "111"), GUILD_B, author="502")
        await run(engine)
        self.assertEqual(posted(discord), [])
        self.assertEqual(nep.requested, [])

    async def test_both_ways_reaches_a(self):
        engine, nep, discord, _ = await started("both")
        nep.say("1", copy_of(engine, "111"), GUILD_B, "Bから", author="502")
        await run(engine)
        sent = posted(discord)[0]
        self.assertEqual(sent["channel_id"], "111")
        self.assertEqual(sent["username"], "相手さん")

    async def test_opted_out_message_is_not_copied(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A)
        del nep.bodies["1"]   # 外れた人の発言は API に出ない
        await run(engine)
        self.assertEqual(posted(discord), [])

    async def test_other_category_is_ignored_without_reading_it(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "999", GUILD_A)
        await run(engine)
        self.assertEqual(nep.requested, [])

    async def test_edit_updates_copy_but_pin_does_not(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "前")
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        engine.store.set("changes_after", nep.last_seq())
        nep.event("message.update", message_id="1", channel_id="111")   # ピン留めなど
        await run(engine)
        self.assertFalse([item for item in discord.writes if item[0] == "edit"])
        engine.store.set("changes_after", nep.last_seq())
        nep.bodies["1"].update(content="後", edited_at="2026-11-09T12:00:00+00:00")
        nep.event("message.update", message_id="1", channel_id="111")
        await run(engine)
        self.assertIn("後", discord.messages[copy_id]["content"])

    async def test_delete_removes_copy(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A)
        await run(engine)
        engine.store.set("changes_after", nep.last_seq())
        nep.event("message.delete", message_id="1")
        await run(engine)
        self.assertEqual(posted(discord), [])
        self.assertIsNone(engine.store.message_by_source("1"))

    async def test_deleting_a_copy_keeps_the_original(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A)
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        engine.store.set("changes_after", nep.last_seq())
        discord.writes.clear()
        nep.event("message.delete", message_id=copy_id)
        await run(engine)
        self.assertFalse([item for item in discord.writes if item[0] == "delete_message"])

    async def test_reply_quotes_and_links_to_the_copy(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "元の発言")
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        engine.store.set("changes_after", nep.last_seq())
        nep.say("2", "111", GUILD_A, "返事", reply_to_id="1")
        await run(engine)
        text = posted(discord)[1]["content"]
        self.assertIn("元の発言", text)
        self.assertIn(f"/channels/{GUILD_B}/{copy_of(engine, '111')}/{copy_id}", text)
        self.assertIn("返事", text)

    async def test_reply_to_a_copy_links_to_the_original(self):
        engine, nep, discord, _ = await started("both")
        target = copy_of(engine, "111")
        nep.say("1", "111", GUILD_A, "Aの発言")
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        engine.store.set("changes_after", nep.last_seq())
        nep.say("2", target, GUILD_B, "Bの返事", author="502", reply_to_id=copy_id)
        await run(engine)
        text = posted(discord)[1]["content"]
        self.assertIn("Aの発言", text)
        self.assertIn(f"/channels/{GUILD_A}/111/1", text)

    async def test_reply_to_missing_parent(self):
        engine, nep, discord, _ = await started()
        nep.say("2", "111", GUILD_A, "返事", reply_to_id="404")
        await run(engine)
        self.assertIn("表示できません", posted(discord)[0]["content"])

    async def test_spoiler_attachment_stays_hidden(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "", attachments=[{"url": "https://example.invalid/x", "extension": "png", "spoiler": 1, "size": 10, "content_type": "image/png"}])
        await run(engine)
        self.assertEqual(posted(discord)[0]["files"][0]["filename"], "SPOILER_file0.png")

    async def test_too_large_attachment_becomes_a_note(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "本文", attachments=[{"url": "https://example.invalid/x", "extension": "mp4", "spoiler": 0, "size": 999 * 1024 * 1024}])
        await run(engine)
        sent = posted(discord)[0]
        self.assertEqual(sent["files"], [])
        self.assertIn("20 MB を超える", sent["content"])

    async def test_silent_flag_is_copied_and_voice_flag_is_not(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, flags=4096 | 8192)
        await run(engine)
        self.assertEqual(posted(discord)[0]["flags"], 4096)

    async def test_late_message_gets_a_mark(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, created_at="2026-11-09T08:00:00+00:00")
        nep.bodies["1"]["created_at"] = "2026-11-09T08:00:00+00:00"
        await run(engine)
        self.assertIn("遅れて同期", posted(discord)[0]["content"])

    async def test_expired_poll_is_noted_not_lost(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "", poll={"question": "架空の質問", "expires_at": "2020-01-01T00:00:00+00:00"})
        await run(engine)
        self.assertIn("架空の質問", posted(discord)[0]["content"])

    async def test_deleted_webhook_is_recreated_and_reported(self):
        engine, nep, discord, notices = await started()
        nep.say("1", "111", GUILD_A)
        await run(engine)
        discord.webhooks.clear()
        engine.store.set("changes_after", nep.last_seq())
        nep.say("2", "111", GUILD_A)
        await run(engine)
        self.assertEqual(len(posted(discord)), 2)
        self.assertTrue(notices)

    async def test_webhook_is_reused_across_restarts(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A)
        await run(engine)
        engine.store.drop_webhook(copy_of(engine, "111"))
        engine.store.set("changes_after", nep.last_seq())
        nep.say("2", "111", GUILD_A)
        await run(engine)
        self.assertEqual(len(discord.webhooks), 1)

    async def test_first_run_starts_from_the_latest(self):
        engine, nep, discord, _ = make_world()
        nep.say("1", "111", GUILD_A, "始める前の発言")
        await engine.run_once()
        self.assertEqual(posted(discord), [])
        nep.say("2", "111", GUILD_A, "始めたあとの発言")
        await engine.run_once()
        self.assertEqual(len(posted(discord)), 1)
        self.assertIn("始めたあと", posted(discord)[0]["content"])


class ThreadTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_channel_thread_is_created_by_the_bot(self):
        engine, nep, discord, _ = await started()
        nep.tables["threads"].append({"thread_id": "300", "channel_id": "111", "title": "架空のスレッド", "tags": "[]", "archived": 0, "locked": 0})
        nep.say("1", "111", GUILD_A, thread_id="300")
        nep.say("2", "111", GUILD_A, thread_id="300")
        await run(engine)
        creates = [item for item in discord.writes if item[0] == "create_thread"]
        self.assertEqual(len(creates), 1)
        self.assertEqual({item["channel_id"] for item in posted(discord)}, {engine.store.thread_side("300")[1]})

    async def test_forum_post_is_created_by_webhook_with_tags_by_name(self):
        engine, nep, discord, _ = make_world()
        discord.add("120", GUILD_A, 15, "掲示板", CAT_A, available_tags=[{"id": "1", "name": "質問"}, {"id": "2", "name": "雑談"}])
        nep.tables["channels"].append({"channel_id": "120", "category_id": CAT_A, "kind": "forum", "name": "掲示板", "reading": 1, "guild_id": GUILD_A})
        nep.tables["threads"].append({"thread_id": "400", "channel_id": "120", "title": "架空の投稿", "tags": '["質問"]', "archived": 0, "locked": 0})
        await engine.sync_structure()
        engine.store.set("changes_after", nep.last_seq())
        nep.say("1", "120", GUILD_A, "最初", thread_id="400")
        nep.say("2", "120", GUILD_A, "続き", thread_id="400")
        await run(engine)
        target_forum = copy_of(engine, "120")
        thread = discord.channels[engine.store.thread_side("400")[1]]
        self.assertEqual((thread["parent_id"], thread["name"]), (target_forum, "架空の投稿"))
        wanted = next(tag["id"] for tag in discord.channels[target_forum]["available_tags"] if tag["name"] == "質問")
        self.assertEqual(thread["applied_tags"], [wanted])
        self.assertEqual(len([item for item in discord.channels.values() if item["type"] == 11]), 1)

    async def test_thread_title_and_lock_flow(self):
        engine, nep, discord, _ = await started()
        info = {"thread_id": "300", "channel_id": "111", "title": "前", "tags": "[]", "archived": 0, "locked": 0}
        nep.tables["threads"].append(info)
        nep.say("1", "111", GUILD_A, thread_id="300")
        await run(engine)
        info.update(title="後", locked=1)
        await engine.handle_thread_update("300")
        copy = discord.channels[engine.store.thread_side("300")[1]]
        self.assertEqual((copy["name"], copy["locked"]), ("後", True))
        discord.writes.clear()
        await engine.handle_thread_update("300")
        self.assertEqual(discord.writes, [])

    async def test_thread_delete_removes_the_copy_only_when_really_gone(self):
        engine, nep, discord, _ = await started()
        nep.tables["threads"].append({"thread_id": "300", "channel_id": "111", "title": "前", "tags": "[]", "archived": 0, "locked": 0})
        nep.say("1", "111", GUILD_A, thread_id="300")
        await run(engine)
        copy = engine.store.thread_side("300")[1]
        await engine.handle_thread_delete("300")     # 元が消えたことは 404 で確かめる（ここでは元のスレッドは偽物に無い）
        self.assertNotIn(copy, discord.channels)


class ForgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_opted_out_user_copies_are_removed_idempotently(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, author="501")
        nep.say("2", "111", GUILD_A, author="777")
        await run(engine)
        nep.forget_rows = [{"seq": 1, "kind": "user", "id": "501"}, {"seq": 2, "kind": "user", "id": "501"}, {"seq": 3, "kind": "user", "id": "知らない人"}]
        await engine.process_forget()
        self.assertEqual(len(posted(discord)), 1)
        self.assertIsNone(engine.store.message_by_source("1"))
        self.assertIsNotNone(engine.store.message_by_source("2"))

    async def test_forget_continues_from_the_cursor(self):
        engine, nep, discord, _ = await started()
        nep.forget_rows = [{"seq": 5, "kind": "message", "id": "知らない発言"}]
        await engine.process_forget()
        self.assertEqual(engine.store.get("forget_after"), "5")

    async def test_webhook_gone_is_reported_with_the_copy_id(self):
        engine, nep, discord, notices = await started()
        nep.say("1", "111", GUILD_A, author="501")
        await run(engine)
        copy_id = posted(discord)[0]["id"]
        discord.webhooks.clear()
        nep.forget_rows = [{"seq": 1, "kind": "user", "id": "501"}]
        await engine.process_forget()
        self.assertTrue(any(copy_id in item for item in notices))


class SwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_messages_can_be_switched_off(self):
        engine, nep, discord, _ = await started(sync_messages=False)
        nep.say("1", "111", GUILD_A)
        await run(engine)
        self.assertEqual(posted(discord), [])
        self.assertEqual(nep.requested, [])

    async def test_failure_on_one_row_is_reported_and_does_not_block(self):
        engine, nep, discord, notices = await started()
        discord.fail("send", forbidden())
        nep.say("1", "111", GUILD_A)
        nep.say("2", "111", GUILD_A)
        await run(engine)
        self.assertEqual(len(posted(discord)), 1)
        self.assertTrue(notices)


class LateEditTest(unittest.IsolatedAsyncioTestCase):
    def _copy_posted_at(self, engine, source_id, when):
        """写しの ID を、その時刻に作られた ID に置き換える。"""
        snowflake = str((int(when.timestamp() * 1000) - 1420070400000) << 22)
        engine.store.db.execute("UPDATE message_pairs SET copy_id = ? WHERE source_id = ?", (snowflake, source_id))
        engine.store.db.commit()
        return snowflake

    async def _edit_late(self, engine, nep, discord, copy_id):
        discord.messages[copy_id] = {"id": copy_id, "content": ""}
        nep.bodies["1"].update(content="編集後", edited_at="2026-11-09T15:00:00+00:00")
        engine.now = lambda: NOW + timedelta(hours=3)
        engine.store.set("changes_after", nep.last_seq())
        nep.event("message.update", message_id="1", channel_id="111")
        await run(engine)
        return discord.messages[copy_id]["content"]

    async def test_editing_long_after_does_not_add_the_delay_mark(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A)
        await run(engine)
        copy_id = self._copy_posted_at(engine, "1", NOW)
        self.assertNotIn("遅れて同期", await self._edit_late(engine, nep, discord, copy_id))

    async def test_a_copy_that_was_really_late_keeps_its_mark_after_editing(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A)
        await run(engine)
        copy_id = self._copy_posted_at(engine, "1", NOW + timedelta(hours=2))
        self.assertIn("遅れて同期", await self._edit_late(engine, nep, discord, copy_id))


class AttachmentLimitTest(unittest.IsolatedAsyncioTestCase):
    async def send(self, size):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "本文", attachments=[{"url": "https://cdn.discordapp.com/attachments/1/2/report_v2.pdf?ex=1", "extension": "pdf", "spoiler": 0, "size": size}])
        await run(engine)
        return posted(discord)[0]

    async def test_exactly_20_mib_is_sent_with_its_name(self):
        sent = await self.send(20 * 1024 * 1024)
        self.assertEqual(sent["files"][0]["filename"], "report_v2.pdf")

    async def test_over_20_mib_is_a_note(self):
        sent = await self.send(20 * 1024 * 1024 + 1)
        self.assertEqual(sent["files"], [])
        self.assertIn("20 MB を超える", sent["content"])

    async def test_title_from_neplector_is_used_when_it_arrives(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "本文", attachments=[{"url": "https://cdn.discordapp.com/attachments/1/2/3_.pdf?ex=1", "extension": "pdf", "spoiler": 0, "size": 10, "title": "第3回 議事録"}])
        await run(engine)
        self.assertEqual(posted(discord)[0]["files"][0]["filename"], "第3回 議事録.pdf")

    async def test_filename_from_neplector_is_used(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "本文", attachments=[{"url": "https://cdn.discordapp.com/attachments/1/2/3_.pdf?ex=1", "extension": "pdf", "spoiler": 0, "size": 10, "filename": "第3回 議事録.pdf"}])
        await run(engine)
        self.assertEqual(posted(discord)[0]["files"][0]["filename"], "第3回 議事録.pdf")

    async def test_old_attachment_with_null_filename_still_goes(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "本文", attachments=[{"url": None, "extension": "mp4", "spoiler": 0, "size": 10, "filename": None}])
        await run(engine)
        self.assertEqual(posted(discord)[0]["files"][0]["filename"], "file0.mp4")

    async def test_limit_does_not_depend_on_boosts(self):
        from nepsync.config import Config
        self.assertEqual(Config(guild_a="1", guild_b="2", category_names=("x",)).max_upload_bytes, 20 * 1024 * 1024)


class RosterLaterTest(unittest.IsolatedAsyncioTestCase):
    async def test_name_is_used_as_soon_as_the_roster_arrives(self):
        engine, nep, discord, _ = await started("both")
        target = copy_of(engine, "111")
        nep.say("1", target, GUILD_B, "名簿が来る前", author="600")
        await run(engine)
        self.assertEqual(posted(discord)[0]["username"], "メンバー")
        self.assertNotIn("avatar_url", posted(discord)[0])
        nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": "600", "display_name": "あとから来た名前", "avatar_url": "https://example.invalid/b.png"})
        engine.store.set("changes_after", nep.last_seq())
        nep.say("2", target, GUILD_B, "名簿が来たあと", author="600")
        await run(engine)
        self.assertEqual(posted(discord)[1]["username"], "あとから来た名前")
        self.assertEqual(posted(discord)[1]["avatar_url"], "https://example.invalid/b.png")


class KeepRunningTest(unittest.IsolatedAsyncioTestCase):
    async def test_structure_failure_does_not_stop_messages(self):
        engine, nep, discord, notices = await started()
        discord.add("112", GUILD_A, 0, "新しい部屋", CAT_A)
        nep.tables["channels"].append({"channel_id": "112", "category_id": CAT_A, "kind": "text", "name": "新しい部屋", "reading": 1, "guild_id": GUILD_A})
        discord.fail("create_channel", forbidden(50013, "Missing Permissions"))
        engine._last_structure = 0
        nep.say("1", "111", GUILD_A)
        await engine.run_once(wait=0)
        self.assertEqual(len(posted(discord)), 1)
        self.assertTrue(any("構造の見回り" in item for item in notices))

    async def test_server_not_ready_postpones_structure_without_notice(self):
        engine, nep, discord, notices = await started()
        discord.unavailable.add(GUILD_B)
        engine._last_structure = 0
        nep.say("1", "111", GUILD_A)
        await engine.run_once(wait=0)
        self.assertEqual(len(posted(discord)), 1)
        self.assertEqual(notices, [])

    async def _forever(self, engine, failures):
        calls = []

        class Stop(BaseException):
            pass

        async def once(wait=25):
            calls.append(1)
            if len(calls) > len(failures):
                raise Stop
            raise failures[len(calls) - 1]
        engine.run_once = once
        pauses = []

        async def sleep(seconds):
            pauses.append(seconds)
        with self.assertRaises(Stop):
            await engine.run_forever(sleep=sleep)
        return pauses

    async def test_transient_waits_longer_each_time(self):
        engine, *_ = await started()
        self.assertEqual(await self._forever(engine, [Transient("x"), Transient("x"), OSError("x")]), [5.0, 10.0, 20.0])

    async def _forever_with_clock(self, engine, outcomes):
        """outcomes の None は成功。sleep のたびに時計を進める。"""
        from datetime import datetime, timedelta, timezone
        clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        engine.now = lambda: clock[0]
        calls = []

        class Stop(BaseException):
            pass

        async def once(wait=25):
            calls.append(1)
            if len(calls) > len(outcomes):
                raise Stop
            if outcomes[len(calls) - 1] is not None:
                raise outcomes[len(calls) - 1]
        engine.run_once = once

        async def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
        with self.assertRaises(Stop):
            await engine.run_forever(sleep=sleep)

    async def test_long_outage_is_told_once_and_recovery_is_told(self):
        engine, nep, discord, notices = await started()
        # 5+10+20+40+80+160+300+300 秒 = 915 秒続いたところで1回だけ知らせる
        await self._forever_with_clock(engine, [Transient("x")] * 12 + [None])
        self.assertEqual(len(notices), 2)
        self.assertIn("15分つながらず", notices[0])
        self.assertIn("戻りました", notices[1])

    async def test_short_outage_is_not_told(self):
        engine, nep, discord, notices = await started()
        await self._forever_with_clock(engine, [Transient("x")] * 5 + [None, Transient("x")] + [None])
        self.assertEqual(notices, [])

    async def test_api_error_and_bug_keep_the_process_alive_and_report(self):
        engine, nep, discord, notices = await started()
        await self._forever(engine, [NeplectorError(401, "x"), ValueError("本文かもしれない値")])
        self.assertEqual(len(notices), 2)
        self.assertFalse(any("本文かもしれない値" in item for item in notices))


class LockTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_process_cannot_start(self):
        import tempfile
        from pathlib import Path

        from nepsync.__main__ import AlreadyRunning, hold_lock
        with tempfile.TemporaryDirectory() as folder:
            first = hold_lock(Path(folder) / "nepsync.lock")
            with self.assertRaises(AlreadyRunning):
                hold_lock(Path(folder) / "nepsync.lock")
            first.close()
            hold_lock(Path(folder) / "nepsync.lock").close()


class SecondSiteOldChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_name_outside_the_category_is_ignored_and_can_be_deleted(self):
        engine, nep, discord, _ = make_world("both")
        discord.add("290", GUILD_B, 0, "雑談", "299")   # 相手のサーバーの、別のカテゴリの同じ名前
        nep.tables["channels"].append({"channel_id": "290", "category_id": "299", "kind": "text", "name": "雑談", "reading": 1, "guild_id": GUILD_B})
        await engine.sync_structure()
        self.assertNotEqual(copy_of(engine, "111"), "290")
        self.assertIsNone(engine.store.channel_side("290"))
        discord.writes.clear()
        del discord.channels["290"]
        nep.event("channel.delete", channel_id="290")
        await engine.handle_row(nep.rows[-1], None)
        self.assertEqual(discord.writes, [])
        self.assertIn("111", discord.channels)


if __name__ == "__main__":
    unittest.main()
