"""ピン留めのテスト（ADR 0012）。データはすべて架空。"""

import os
import sqlite3
import tempfile
import unittest

from discord import HTTPException

from nepsync.store import Store

from .fakes import GUILD_A, GUILD_B, copy_of, forbidden, http_error, make_world, not_found


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


async def copied(engine, nep, discord, message_id="1", **extra):
    """発言を流して、写しの ID を返す。写しも Neplector に記録された形にする。"""
    nep.say(message_id, "111", GUILD_A, "架空の発言", **extra)
    await run(engine)
    copy_id = engine.store.message_by_source(message_id)["copy_id"]
    nep.bodies[copy_id] = {"message_id": copy_id, "channel_id": copy_of(engine, "111"), "thread_id": copy_of(engine, "111"),
                           "guild_id": GUILD_B, "edited_at": None, "pinned": 0, "poll": None, "reactions": [], "webhook_id": "7"}
    discord.writes.clear()
    return copy_id


def pins(discord):
    return [item for item in discord.writes if item[0] in ("pin", "unpin")]


def set_pin(nep, message_id, value):
    """Neplector に、ピン留めが変わった記録と知らせが届いた形にする。"""
    nep.bodies[message_id]["pinned"] = int(value)
    nep.event("message.update", message_id=message_id, channel_id=nep.bodies[message_id]["channel_id"])


class PinTest(unittest.IsolatedAsyncioTestCase):
    async def test_pin_on_original_pins_the_copy(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        self.assertEqual(pins(discord), [("pin", GUILD_B, copy_of(engine, "111"), copy_id)])
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 1)

    async def test_pin_is_not_an_edit(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        self.assertFalse([item for item in discord.writes if item[0] == "edit"])

    async def test_unpin_on_original_unpins_the_copy(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        discord.writes.clear()
        set_pin(nep, "1", False)
        await run(engine)
        self.assertEqual(pins(discord), [("unpin", GUILD_B, copy_of(engine, "111"), copy_id)])
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 0)

    async def test_own_pin_coming_back_does_nothing(self):
        engine, nep, discord, _ = await started()
        copy_id = await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        discord.writes.clear()
        set_pin(nep, copy_id, True)   # bot が写しに付けた知らせ
        await run(engine)
        self.assertEqual(pins(discord), [])

    async def test_pin_on_copy_pins_the_original_both_ways(self):
        engine, nep, discord, _ = await started("both")
        copy_id = await copied(engine, nep, discord)
        set_pin(nep, copy_id, True)
        await run(engine)
        self.assertEqual(pins(discord), [("pin", GUILD_A, "111", "1")])
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 1)

    async def test_one_way_does_not_touch_the_original(self):
        engine, nep, discord, _ = await started("a_to_b")
        copy_id = await copied(engine, nep, discord)
        set_pin(nep, copy_id, True)
        await run(engine)
        self.assertEqual(pins(discord), [])
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 0)

    async def test_stale_record_of_the_other_side_is_not_trusted(self):
        """写しをピン留めした直後、Neplector の写しの記録がまだ古いまま、元の発言が編集されても、元のピン留めを外さない。"""
        engine, nep, discord, _ = await started("both")
        await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        discord.writes.clear()
        nep.bodies["1"]["edited_at"] = "2026-11-09T12:00:00+00:00"
        nep.event("message.update", message_id="1", channel_id="111")
        await run(engine)
        self.assertEqual(pins(discord), [])

    async def test_later_operation_wins(self):
        """元の発言に付けたあと、写しで外されたら、元も外す。"""
        engine, nep, discord, _ = await started("both")
        copy_id = await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        discord.writes.clear()
        set_pin(nep, copy_id, False)
        await run(engine)
        self.assertEqual(pins(discord), [("unpin", GUILD_A, "111", "1")])

    async def test_pin_in_thread_goes_to_the_copy_thread(self):
        engine, nep, discord, _ = await started()
        nep.tables["threads"].append({"thread_id": "300", "channel_id": "111", "title": "架空のスレッド", "tags": "[]", "archived": 0, "locked": 0})
        nep.say("1", "111", GUILD_A, "スレッドの中", thread_id="300")
        await run(engine)
        pair = engine.store.message_by_source("1")
        discord.writes.clear()
        set_pin(nep, "1", True)
        await run(engine)
        self.assertEqual(pins(discord), [("pin", GUILD_B, pair["target_thread"], pair["copy_id"])])

    async def test_already_pinned_message_is_pinned_when_copied(self):
        engine, nep, discord, _ = await started()
        nep.say("1", "111", GUILD_A, "架空の発言", pinned=1)
        await run(engine)
        copy_id = engine.store.message_by_source("1")["copy_id"]
        self.assertEqual(pins(discord), [("pin", GUILD_B, copy_of(engine, "111"), copy_id)])
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 1)

    async def test_missing_permission_is_reported_and_retried_later(self):
        engine, nep, discord, notices = await started()
        await copied(engine, nep, discord)
        discord.fail("pin", forbidden(50013, "Missing Permissions"))
        set_pin(nep, "1", True)
        await run(engine)
        self.assertTrue(any("メッセージのピン留め" in text for text in notices))
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 0)
        nep.event("message.update", message_id="1", channel_id="111")   # 次の知らせで、もう一度やる
        await run(engine)
        self.assertEqual(pins(discord)[-1][0], "pin")
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 1)

    async def test_pin_limit_is_reported(self):
        engine, nep, discord, notices = await started()
        await copied(engine, nep, discord)
        discord.fail("pin", http_error(HTTPException, 400, 30003, "Maximum number of pins reached (250)"))
        set_pin(nep, "1", True)
        await run(engine)
        self.assertTrue(any("上限" in text for text in notices))
        self.assertEqual(engine.store.message_by_source("1")["pinned"], 0)

    async def test_deleted_copy_is_not_an_error(self):
        engine, nep, discord, notices = await started()
        await copied(engine, nep, discord)
        discord.fail("pin", not_found(10008, "Unknown Message"))
        set_pin(nep, "1", True)
        await run(engine)
        self.assertEqual(notices, [])

    async def test_opted_out_message_is_ignored(self):
        engine, nep, discord, _ = await started()
        await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        del nep.bodies["1"]   # 外れた人の発言は API に出ない
        await run(engine)
        self.assertEqual(pins(discord), [])

    async def test_switch_off(self):
        engine, nep, discord, _ = await started(sync_pins=False)
        await copied(engine, nep, discord)
        set_pin(nep, "1", True)
        await run(engine)
        self.assertEqual(pins(discord), [])

    async def test_update_of_unsynced_message_does_not_read_neplector(self):
        engine, nep, discord, _ = await started()
        nep.event("message.update", message_id="5", channel_id="999")
        await run(engine)
        self.assertEqual(nep.requested, [])

    async def test_old_file_gets_the_pin_column_as_unpinned(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE message_pairs (source_id TEXT PRIMARY KEY, copy_id TEXT NOT NULL UNIQUE, author_id TEXT NOT NULL, "
                        "target_channel TEXT NOT NULL, target_thread TEXT, source_thread TEXT, webhook_id TEXT NOT NULL, edited_at TEXT)")
            old.execute("INSERT INTO message_pairs VALUES ('1', '2', '501', '211', NULL, NULL, '7', NULL)")
            old.commit()
            old.close()
            store = Store(path)
            self.assertEqual(store.message_by_source("1")["pinned"], 0)
            store.save_message_pair("3", "4", "501", "211", None, None, "7", None)
            self.assertEqual(store.message_by_source("3")["pinned"], 0)
            store.db.close()


if __name__ == "__main__":
    unittest.main()
