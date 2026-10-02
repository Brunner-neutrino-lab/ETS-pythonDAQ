import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from daq import labbook


class FakeInflux:
    """Stands in for HUB.sc: records what the mirror writes and deletes."""

    def __init__(self):
        self.written = []
        self.deleted = []
        self._cfg = SimpleNamespace(influxdb_bucket="slowcontrol",
                                    influxdb_org="ets")
        self._client = self

    def write_api(self, write_options=None):
        return SimpleNamespace(
            write=lambda bucket, org, record: self.written.append(
                (bucket, org, record.to_line_protocol())))

    def delete_api(self):
        return SimpleNamespace(
            delete=lambda **kwargs: self.deleted.append(kwargs))


class LabbookTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.entries_path = os.path.join(tmp.name, "labbook_entries.jsonl")
        self.history_path = os.path.join(tmp.name, "labbook_history.jsonl")
        self.attach_dir = os.path.join(tmp.name, "labbook_attachments")
        os.makedirs(self.attach_dir)
        for name, value in (("_ENTRIES_PATH", self.entries_path),
                            ("_HISTORY_PATH", self.history_path),
                            ("_ATTACH_DIR", self.attach_dir),
                            ("_paste_queues", {})):
            patcher = patch.object(labbook, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def history(self):
        if not os.path.exists(self.history_path):
            return []
        with open(self.history_path) as f:
            return [json.loads(line) for line in f]


class TestEditAndDelete(LabbookTestCase):
    def test_update_replaces_content_and_keeps_identity(self):
        first, _ = labbook.append("Lei", "first", "body 1", [])
        target, _ = labbook.append("Lei", "typo", "body 2", ["a.png"])
        last, _ = labbook.append("lucas", "last", "body 3", [])

        updated, mirrored = labbook.update(
            target["id"], "fixed", "body 2b", ["a.png", "b.png"],
            edited_by="lucas")

        self.assertFalse(mirrored)
        self.assertEqual(updated["subject"], "fixed")
        self.assertEqual(updated["body"], "body 2b")
        self.assertEqual(updated["attachments"], ["a.png", "b.png"])
        self.assertEqual(updated["edited_by"], "lucas")
        self.assertGreaterEqual(updated["edited_ts"], target["ts"])
        for key in ("id", "ts", "user"):
            self.assertEqual(updated[key], target[key])
        # Position in the list is unchanged and the neighbours are intact.
        self.assertEqual(labbook.list_all(), [last, updated, first])

    def test_update_records_the_previous_version(self):
        entry, _ = labbook.append("Lei", "before", "old body", ["a.png"])
        labbook.update(entry["id"], "after", "new body", [], edited_by="lucas")

        (record,) = self.history()
        self.assertEqual(record["action"], "edit")
        self.assertEqual(record["by"], "lucas")
        self.assertEqual(record["entry"], entry)

    def test_delete_removes_only_that_entry_and_records_it(self):
        keep, _ = labbook.append("Lei", "keep", "", [])
        attachment = labbook.save_attachment("plot.png", b"png bytes")
        doomed, _ = labbook.append("Lei", "duplicate", "", [attachment])

        deleted, removed = labbook.delete(doomed["id"], deleted_by="lucas")

        self.assertEqual(deleted, doomed)
        self.assertFalse(removed)
        self.assertEqual(labbook.list_all(), [keep])
        self.assertIsNone(labbook.get(doomed["id"]))
        (record,) = self.history()
        self.assertEqual((record["action"], record["by"], record["entry"]),
                         ("delete", "lucas", doomed))
        # The attachment file stays, so the delete can be undone by hand.
        self.assertTrue(os.path.exists(
            os.path.join(self.attach_dir, attachment)))

    def test_unknown_id_changes_nothing(self):
        entry, _ = labbook.append("Lei", "only", "", [])
        with open(self.entries_path, "rb") as f:
            before = f.read()
        revision = labbook.revision()

        self.assertEqual(
            labbook.update("no-such-id", "x", "y", [], edited_by="lucas"),
            (None, False))
        self.assertEqual(labbook.delete("no-such-id", deleted_by="lucas"),
                         (None, False))

        with open(self.entries_path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.history(), [])
        self.assertEqual(labbook.revision(), revision)
        self.assertEqual(labbook.list_all(), [entry])

    def test_rewrite_carries_over_lines_that_do_not_parse(self):
        first, _ = labbook.append("Lei", "first", "", [])
        with open(self.entries_path, "a") as f:
            f.write("{this line is not json\n")
        second, _ = labbook.append("Lei", "second", "", [])

        labbook.delete(first["id"], deleted_by="lucas")
        labbook.update(second["id"], "second, edited", "", [],
                       edited_by="lucas")

        with open(self.entries_path) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "{this line is not json")
        self.assertEqual(json.loads(lines[1])["subject"], "second, edited")
        self.assertEqual(len(lines), 2)

    def test_every_change_bumps_the_revision(self):
        start = labbook.revision()
        entry, _ = labbook.append("Lei", "s", "b", [])
        self.assertEqual(labbook.revision(), start + 1)
        labbook.update(entry["id"], "s2", "b", [], edited_by="Lei")
        self.assertEqual(labbook.revision(), start + 2)
        labbook.delete(entry["id"], deleted_by="Lei")
        self.assertEqual(labbook.revision(), start + 3)


class TestInfluxMirror(LabbookTestCase):
    def test_edit_overwrites_the_same_point(self):
        influx = FakeInflux()
        entry, mirrored = labbook.append("Lei", "before", "b", [],
                                         slowcontrol=influx)
        self.assertTrue(mirrored)
        _, mirrored = labbook.update(entry["id"], "after", "b", ["a.png"],
                                     edited_by="lucas", slowcontrol=influx)
        self.assertTrue(mirrored)

        (_, _, posted), (bucket, org, edited) = influx.written
        self.assertEqual((bucket, org), ("slowcontrol", "ets"))
        # InfluxDB replaces a point when measurement, tags and timestamp
        # all match: the author tag and the time must survive the edit.
        self.assertTrue(edited.startswith("labbook,user=Lei "))
        self.assertEqual(posted.rsplit(" ", 1)[1], edited.rsplit(" ", 1)[1])
        self.assertIn('subject="after"', edited)
        self.assertIn("n_attachments=1i", edited)

    def test_delete_targets_only_the_entrys_point(self):
        influx = FakeInflux()
        entry, _ = labbook.append("Lei", "s", "b", [], slowcontrol=influx)

        _, removed = labbook.delete(entry["id"], deleted_by="lucas",
                                    slowcontrol=influx)

        self.assertTrue(removed)
        (call,) = influx.deleted
        self.assertEqual(call["predicate"], '_measurement="labbook"')
        self.assertEqual((call["bucket"], call["org"]),
                         ("slowcontrol", "ets"))
        at = datetime.fromtimestamp(entry["ts"], timezone.utc)
        self.assertLess(call["start"], at)
        self.assertGreater(call["stop"], at)
        self.assertLessEqual(
            (call["stop"] - call["start"]).total_seconds(), 0.002)

    def test_without_slowcontrol_the_file_still_changes(self):
        entry, _ = labbook.append("Lei", "s", "b", [])
        disconnected = SimpleNamespace(_client=None)

        _, mirrored = labbook.update(entry["id"], "s2", "b", [],
                                     edited_by="Lei", slowcontrol=disconnected)
        self.assertFalse(mirrored)
        _, removed = labbook.delete(entry["id"], deleted_by="Lei",
                                    slowcontrol=disconnected)
        self.assertFalse(removed)
        self.assertEqual(labbook.list_all(), [])

    def test_failed_influx_delete_does_not_block_the_delete(self):
        influx = FakeInflux()
        entry, _ = labbook.append("Lei", "s", "b", [], slowcontrol=influx)

        def refuse():
            raise RuntimeError("token has no write access")
        influx.delete_api = refuse

        with self.assertLogs("daq.labbook", level="WARNING"):
            deleted, removed = labbook.delete(
                entry["id"], deleted_by="lucas", slowcontrol=influx)

        self.assertEqual(deleted, entry)
        self.assertFalse(removed)
        self.assertEqual(labbook.list_all(), [])


class TestPasteQueue(LabbookTestCase):
    def test_a_paste_is_only_seen_by_the_page_it_came_from(self):
        labbook.queue_pasted("lei-page", "one.png")
        labbook.queue_pasted("lei-page", "two.png")

        # Every open page polls; another user's page must come up empty.
        self.assertEqual(labbook.pop_pasted("lucas-page"), [])
        self.assertEqual(labbook.pop_pasted("lei-page"),
                         ["one.png", "two.png"])
        self.assertEqual(labbook.pop_pasted("lei-page"), [])


if __name__ == "__main__":
    unittest.main()
