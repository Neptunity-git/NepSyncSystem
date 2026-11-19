"""リアクションと投票のテスト。データはすべて架空。"""

import unittest
from datetime import timedelta

from .fakes import GUILD_A, GUILD_B, NOW, copy_of, forbidden, make_world


async def started(direction="both", **options):
    engine, nep, discord, notices = make_world(direction, **options)
    await engine.sync_structure()
    engine.store.set("changes_after", nep.last_seq())
    discord.writes.clear()
    return engine, nep, discord, notices


async def run(engine):
    rows = [row for row in engine.nep.rows if row["seq"] > int(engine.store.get("changes_after") or 0)]
    await engine.handle_batch(rows)
    engine.store.set("changes_after", engine.nep.last_seq())


async def copied(engine, nep, message_id="1", content="架空の発言", **extra):
    """発言を流して、写しの ID を返す。写しも Neplector に記録された形にする。"""
    nep.say(message_id, "111", GUILD_A, content, **extra)
    await run(engine)
    copy_id = engine.store.message_by_source(message_id)["copy_id"]
    nep.bodies[copy_id] = {"message_id": copy_id, "channel_id": copy_of(engine, "111"), "reactions": [], "poll": None, "webhook_id": "7"}
    return copy_id


def react(nep, message_id, emoji, user="501"):
    nep.bodies[message_id]["reactions"].append({"emoji": emoji, "user_id": user, "burst": 0})
    nep.event("reaction.add", message_id=message_id, emoji=emoji, user_id=user)


def unreact(nep, message_id, emoji, user="501"):
    nep.bodies[message_id]["reactions"] = [item for item in nep.bodies[message_id]["reactions"] if not (item["emoji"] == emoji and item["user_id"] == user)]
    nep.event("reaction.remove", message_id=message_id, emoji=emoji, user_id=user)


class ReactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_on_the_original_reaches_the_copy(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "😀")
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], {"😀"})

    async def test_reaction_on_the_copy_reaches_the_original(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, copy_id, "👍", user="502")
        await run(engine)
        self.assertEqual(discord.reactions["1"], {"👍"})

    async def test_one_way_does_not_write_to_the_original(self):
        engine, nep, discord, _ = await started("a_to_b")
        copy_id = await copied(engine, nep)
        react(nep, copy_id, "👍", user="502")
        await run(engine)
        self.assertNotIn("1", discord.reactions)

    async def test_custom_emoji_is_matched_by_name(self):
        engine, nep, discord, _ = await started()
        nep.tables["emojis"].append({"emoji_id": "9002", "guild_id": GUILD_B, "name": "logo", "animated": 0, "available": 1})
        copy_id = await copied(engine, nep)
        react(nep, "1", "logo:9001")
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], {"logo:9002"})

    async def test_missing_custom_emoji_is_skipped_and_reported_then_used_when_added(self):
        engine, nep, discord, notices = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "only_here:9001")
        await run(engine)
        self.assertNotIn(copy_id, discord.reactions)
        self.assertTrue(any("only_here" in item for item in notices))
        nep.tables["emojis"].append({"emoji_id": "9002", "guild_id": GUILD_B, "name": "only_here", "animated": 0, "available": 1})
        react(nep, "1", "only_here:9001", user="777")
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], {"only_here:9002"})

    async def test_removed_only_when_nobody_is_left(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "😀", user="501")
        react(nep, "1", "😀", user="777")
        await run(engine)
        unreact(nep, "1", "😀", user="501")
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], {"😀"})
        unreact(nep, "1", "😀", user="777")
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], set())

    async def test_opted_out_person_leaving_removes_the_reaction(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "😀")
        await run(engine)
        nep.bodies["1"]["reactions"] = []
        nep.event("reaction.remove", message_id="1", emoji="😀", user_id=None)   # 誰のものか消えたとき
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], set())

    async def test_clear_on_the_copy_puts_the_bot_back_while_the_original_has_it(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "😀")
        await run(engine)
        discord.reactions[copy_id].clear()   # 相手のサーバーで誰かがまとめて消した
        nep.event("reaction.clear", message_id=copy_id, emoji=None)
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], {"😀"})

    async def test_clear_on_the_original_removes_from_the_copy(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "😀")
        react(nep, "1", "👍")
        await run(engine)
        nep.bodies["1"]["reactions"] = []
        nep.event("reaction.clear", message_id="1", emoji=None)
        await run(engine)
        self.assertEqual(discord.reactions[copy_id], set())

    async def test_repeating_changes_nothing(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep)
        react(nep, "1", "😀")
        await run(engine)
        discord.writes.clear()
        engine._reactions_done = set()
        await engine.sync_reactions("1")
        self.assertEqual(discord.writes, [])

    async def test_reaction_on_an_unpaired_message_is_ignored(self):
        engine, nep, discord, _ = await started()
        nep.event("reaction.add", message_id="404", emoji="😀", user_id="501")
        await run(engine)
        self.assertEqual(discord.writes, [])
        self.assertEqual(nep.requested, [])

    async def test_deleted_pair_forgets_the_bot_reactions(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep)
        react(nep, "1", "😀")
        await run(engine)
        nep.event("message.delete", message_id="1")
        await run(engine)
        self.assertEqual(engine.store.bot_reactions(copy_id), [])

    async def test_missing_permission_is_reported_and_does_not_stop(self):
        engine, nep, discord, notices = await started()
        await copied(engine, nep)
        discord.fail("add_reaction", forbidden(50013, "Missing Permissions"))
        react(nep, "1", "😀")
        nep.say("2", "111", GUILD_A, "次の発言")
        await run(engine)
        self.assertIsNotNone(engine.store.message_by_source("2"))
        self.assertTrue(any("reaction.add" in item for item in notices))


class RemovedReactorNamesTest(unittest.IsolatedAsyncioTestCase):
    async def test_reactions_no_longer_edit_the_copy(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="本文")
        discord.writes.clear()
        react(nep, "1", "😀")
        await run(engine)
        self.assertEqual([item for item in discord.writes if item[0] == "edit"], [])
        self.assertEqual(discord.reactions[copy_id], {"😀"})

    async def test_names_written_before_are_removed_once(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="本文")
        discord.messages[copy_id]["content"] = "本文\n-# 😀 架空さん"   # やめた機能が書いた形
        engine.store.db.execute("INSERT INTO reactor_lines VALUES (?, ?)", ("1", "x"))
        engine.store.db.commit()
        react(nep, "1", "😀")
        await engine.periodic()
        self.assertEqual(discord.messages[copy_id]["content"], "本文")
        self.assertEqual(engine.store.reactor_line_sources(), [])
        discord.writes.clear()
        await engine.periodic()
        self.assertEqual([item for item in discord.writes if item[0] == "edit"], [])

    async def test_clean_up_skips_what_cannot_be_edited(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="本文")
        for source in ("1", "404"):
            engine.store.db.execute("INSERT INTO reactor_lines VALUES (?, ?)", (source, "x"))
        engine.store.db.commit()
        del nep.bodies["1"]
        await engine.periodic()
        self.assertEqual(engine.store.reactor_line_sources(), [])


def poll(expires=NOW + timedelta(hours=5, minutes=10), votes=None):
    return {"question": "架空の質問", "expires_at": expires.isoformat() if expires else None, "multiselect": 0,
            "answers": [{"answer_id": 1, "text": "はい", "emoji": None}, {"answer_id": 2, "text": "いいえ", "emoji": "😀"}], "votes": votes or []}


def tally_ids(engine, source_id="1"):
    row = engine.store.tally(source_id)
    return row["source_tally"], row["copy_tally"]


class PollTest(unittest.IsolatedAsyncioTestCase):
    async def test_poll_is_created_on_the_other_side_with_time_rounded_up(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="", poll=poll())
        made = discord.messages[copy_id]["poll"]
        self.assertEqual(made["duration"], 6)
        self.assertEqual(made["question"], {"text": "架空の質問"})
        self.assertEqual(made["answers"][1]["poll_media"], {"text": "いいえ", "emoji": {"id": None, "name": "😀"}})
        self.assertFalse(made["allow_multiselect"])

    async def test_custom_emoji_in_an_answer_is_matched_or_dropped(self):
        engine, nep, discord, _ = await started()
        nep.tables["emojis"].append({"emoji_id": "9002", "guild_id": GUILD_B, "name": "logo", "animated": 0, "available": 1})
        data = poll()
        data["answers"][0]["emoji"] = "logo:9001"
        data["answers"][1]["emoji"] = "none:9003"
        copy_id = await copied(engine, nep, content="", poll=data)
        answers = discord.messages[copy_id]["poll"]["answers"]
        self.assertEqual(answers[0]["poll_media"]["emoji"], {"id": 9002, "name": "logo"})
        self.assertNotIn("emoji", answers[1]["poll_media"])

    async def test_expired_poll_becomes_a_note(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="", poll=poll(expires=NOW - timedelta(hours=1)))
        self.assertNotIn("poll", discord.messages[copy_id])
        self.assertIn("架空の質問", discord.messages[copy_id]["content"])
        self.assertIsNone(engine.store.tally("1"))

    async def test_tally_is_posted_on_both_sides_with_numbers_only(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        source_tally, copy_tally = tally_ids(engine)
        for tally in (source_tally, copy_tally):
            self.assertIn("はい：0票", discord.messages[tally]["content"])
            self.assertEqual(discord.messages[tally]["allowed_mentions"], {"parse": []})
        self.assertEqual(discord.messages[source_tally]["channel_id"], "111")
        self.assertEqual(discord.messages[copy_tally]["channel_id"], copy_of(engine, "111"))

    async def test_votes_on_both_sides_are_added_together_after_the_wait(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="", poll=poll())
        source_tally, copy_tally = tally_ids(engine)
        nep.bodies["1"]["poll"]["votes"] = [{"answer_id": 1, "user_id": "501"}]
        nep.bodies[copy_id]["poll"] = {**poll(), "votes": [{"answer_id": 1, "user_id": "502"}, {"answer_id": 2, "user_id": "503"}]}
        nep.event("poll.vote.add", message_id="1", answer_id=1, user_id="501")
        nep.event("poll.vote.add", message_id=copy_id, answer_id=1, user_id="502")
        await run(engine)
        await engine.flush_tallies()
        self.assertIn("はい：0票", discord.messages[source_tally]["content"])   # 10秒たっていない
        engine.now = lambda: NOW + timedelta(seconds=61)
        await engine.flush_tallies()
        for tally in (source_tally, copy_tally):
            text = discord.messages[tally]["content"]
            self.assertIn("はい：2票", text)
            self.assertIn("いいえ：1票", text)
            self.assertNotIn("501", text)

    async def test_same_person_on_the_same_answer_counts_once(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="", poll=poll())
        source_tally, _ = tally_ids(engine)
        nep.bodies["1"]["poll"]["votes"] = [{"answer_id": 1, "user_id": "501"}]
        nep.bodies[copy_id]["poll"] = {**poll(), "votes": [{"answer_id": 1, "user_id": "501"}]}
        nep.event("poll.vote.add", message_id="1", answer_id=1, user_id="501")
        await run(engine)
        engine.now = lambda: NOW + timedelta(seconds=61)
        await engine.flush_tallies()
        self.assertIn("はい：1票", discord.messages[source_tally]["content"])

    async def test_no_rewrite_when_the_numbers_are_the_same(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        nep.bodies["1"]["poll"]["votes"] = [{"answer_id": 1, "user_id": "501"}]
        nep.event("poll.vote.add", message_id="1", answer_id=1, user_id="501")
        await run(engine)
        engine.now = lambda: NOW + timedelta(seconds=61)
        await engine.flush_tallies()
        discord.writes.clear()
        nep.event("poll.vote.add", message_id="1", answer_id=1, user_id="501")
        await run(engine)
        engine.now = lambda: NOW + timedelta(seconds=200)
        await engine.flush_tallies()
        self.assertEqual([item for item in discord.writes if item[0] == "edit"], [])

    async def test_final_update_at_the_deadline_then_stops(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll(expires=NOW + timedelta(hours=1)))
        source_tally, copy_tally = tally_ids(engine)
        engine.now = lambda: NOW + timedelta(hours=2)
        await engine.flush_tallies()
        self.assertIn("締め切りました", discord.messages[source_tally]["content"])
        self.assertIn("締め切りました", discord.messages[copy_tally]["content"])
        discord.writes.clear()
        engine.now = lambda: NOW + timedelta(hours=3)
        await engine.flush_tallies()
        self.assertEqual(discord.writes, [])

    async def test_deleting_the_poll_removes_the_copy_and_both_tallies(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, content="", poll=poll())
        source_tally, copy_tally = tally_ids(engine)
        nep.event("message.delete", message_id="1")
        await run(engine)
        for message_id in (copy_id, source_tally, copy_tally):
            self.assertNotIn(message_id, discord.messages)
        self.assertIsNone(engine.store.tally("1"))

    async def test_opted_out_poll_author_removes_the_tallies(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        source_tally, _ = tally_ids(engine)
        del nep.bodies["1"]
        engine.store.mark_tally_dirty("1")
        engine.now = lambda: NOW + timedelta(seconds=61)
        await engine.flush_tallies()
        self.assertNotIn(source_tally, discord.messages)

    async def test_question_is_not_stored(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        dump = "\n".join(engine.store.db.iterdump())
        self.assertNotIn("架空の質問", dump)
        self.assertNotIn("いいえ", dump)

    async def test_tally_message_is_not_copied_again(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        source_tally, _ = tally_ids(engine)
        count = len(discord.messages)
        nep.say(source_tally, "111", GUILD_A, "合計", author="999", counted=0, webhook_id="7")
        await run(engine)
        self.assertEqual(len(discord.messages), count)

    async def test_polls_can_be_switched_off(self):
        engine, nep, discord, _ = await started(sync_polls=False)
        copy_id = await copied(engine, nep, content="", poll=poll())
        self.assertNotIn("poll", discord.messages[copy_id])
        self.assertIsNone(engine.store.tally("1"))

    async def test_editing_a_poll_message_does_not_touch_the_copy(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        discord.writes.clear()
        nep.bodies["1"]["edited_at"] = "2026-11-09T12:00:00+00:00"
        nep.event("message.update", message_id="1", channel_id="111")
        await run(engine)
        self.assertEqual([item for item in discord.writes if item[0] == "edit"], [])


class TallyWaitTest(unittest.IsolatedAsyncioTestCase):
    """票が入ったら、Neplector を待つ時間を、合計を書き換えてよい時刻までに縮める（ADR 0011 判断1）。"""

    async def test_waits_only_until_the_tally_may_be_rewritten(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        self.assertEqual(engine._tally_wait(25), 25)   # 票が変わっていない
        nep.bodies["1"]["poll"]["votes"] = [{"answer_id": 1, "user_id": "501"}]
        nep.event("poll.vote.add", message_id="1", answer_id=1, user_id="501")
        await run(engine)
        engine.now = lambda: NOW + timedelta(seconds=3)
        self.assertEqual(engine._tally_wait(25), 7)
        engine.now = lambda: NOW + timedelta(seconds=30)
        self.assertEqual(engine._tally_wait(25), 1)

    async def test_vote_reaches_both_tallies_within_ten_seconds(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, content="", poll=poll())
        source_tally, copy_tally = tally_ids(engine)
        waits = []
        changes = nep.changes

        async def recorded(after, limit=500, wait=25):
            waits.append(wait)
            return await changes(after, limit, wait=0)
        nep.changes = recorded
        nep.bodies["1"]["poll"]["votes"] = [{"answer_id": 1, "user_id": "501"}]
        nep.event("poll.vote.add", message_id="1", answer_id=1, user_id="501")
        engine._last_structure = engine._last_forget = NOW.timestamp()
        await engine.run_once()                      # 票を読む
        engine.now = lambda: NOW + timedelta(seconds=10)
        await engine.run_once()                      # 10秒たったので書き換える
        self.assertEqual(waits[1], 25)
        for tally in (source_tally, copy_tally):
            self.assertIn("はい：1票", discord.messages[tally]["content"])


class VotersTest(unittest.IsolatedAsyncioTestCase):
    """右クリックの「投票した人」（ADR 0011 判断2）。押した人にだけ見せる一覧の中身。"""

    async def asyncSetUp(self):
        from .test_mentions_commands import make_commands
        self.engine, self.nep, self.discord, self.notices = await started()
        self.commands = make_commands(self.engine, self.nep, self.discord, self.notices)
        self.copy_id = await copied(self.engine, self.nep, content="", poll=poll())
        self.nep.bodies[self.copy_id].update(guild_id=GUILD_B, poll={**poll(), "votes": []})
        self.nep.tables["member_servers"].append({"guild_id": GUILD_B, "user_id": "503", "display_name": "相手の次郎", "avatar_url": None})

    def vote(self, message_id, answer_id, user):
        self.nep.bodies[message_id]["poll"]["votes"].append({"answer_id": answer_id, "user_id": user})

    async def text(self, message_id, guild=GUILD_A):
        return (await self.commands.voters(message_id, guild))["content"]

    async def test_each_answer_lists_people_by_server(self):
        self.vote("1", 1, "501")
        self.vote(self.copy_id, 1, "502")
        self.vote(self.copy_id, 2, "503")
        lines = (await self.text("1")).split("\n")
        self.assertEqual(lines, ["**投票した人**（両方のサーバー）", "**架空の質問**",
                                 "はい　2人", f"　サーバー{GUILD_A}：架空さん", f"　サーバー{GUILD_B}：相手さん",
                                 "😀 いいえ　1人", f"　サーバー{GUILD_B}：相手の次郎"])

    async def test_own_server_comes_first(self):
        self.vote("1", 1, "501")
        self.vote(self.copy_id, 1, "502")
        lines = (await self.text(self.copy_id, guild=GUILD_B)).split("\n")
        self.assertEqual(lines[3:5], [f"　サーバー{GUILD_B}：相手さん", f"　サーバー{GUILD_A}：架空さん"])

    async def test_same_person_on_the_same_answer_is_one(self):
        self.vote("1", 1, "501")
        self.vote(self.copy_id, 1, "501")
        self.assertIn("はい　1人", await self.text("1"))

    async def test_works_on_the_tally_messages_too(self):
        self.vote(self.copy_id, 2, "503")
        for tally in tally_ids(self.engine):
            self.assertIn("相手の次郎", await self.text(tally))

    async def test_no_votes_yet(self):
        self.assertIn("まだ", await self.text("1"))

    async def test_not_a_poll_or_not_synced(self):
        self.nep.say("2", "111", GUILD_A, "ふつうの発言")
        await run(self.engine)
        self.assertIn("投票ではありません", await self.text("2"))
        self.assertIn("同期している投票ではありません", await self.text("424242"))

    async def test_opted_out_poll_is_not_shown(self):
        del self.nep.bodies["1"]
        self.assertIn("表示できません", await self.text("1"))

    async def test_answer_is_private_and_never_pings(self):
        from types import SimpleNamespace

        from .test_bot import PRIVATE, FakeInteraction, make_bot
        self.vote("1", 1, "501")
        client = make_bot(self.engine, self.commands, self.notices)
        interaction = FakeInteraction()
        await client.show_list(interaction, SimpleNamespace(id=1), self.commands.voters)
        self.assertEqual(interaction.calls[0], PRIVATE)
        self.assertIn("架空さん", interaction.edited["content"])
        self.assertEqual(interaction.edited["allowed_mentions"].to_dict(), {"parse": []})
        await client.close()


if __name__ == "__main__":
    unittest.main()
