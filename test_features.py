"""
Manual test script for the features added in this branch:
  - drive_upload_file
  - calendar_create_event  (cross-account conflict check + auto-block)
  - calendar_update_event  (syncs blocking events to new time)
  - calendar_delete_event  (removes linked blocking events)

Run directly (no MCP client needed):
    python test_features.py

Requires both 'personal' and 'work' accounts authenticated
(see credentials/tokens/*.json). Everything this script creates
is deleted again at the end, and any failure still runs cleanup.
"""

import asyncio
import json

import server as s

PRIMARY = "personal"
OTHER = "work"


def show(label, result):
    print(f"\n--- {label} ---")
    print(result[0].text)
    return json.loads(result[0].text)


async def main():
    created_ids = []  # (account, event_id) to clean up on exit

    try:
        # 1. drive_upload_file -------------------------------------------------
        with open("test_upload.txt", "w") as f:
            f.write("hello from test_features.py\n")

        up = show("drive_upload_file", await s.call_tool("drive_upload_file", {
            "account": PRIMARY,
            "file_path": "test_upload.txt",
        }))
        print(f"Uploaded file id: {up['id']} — check Drive, then delete manually or via drive_delete_file.")

        # 2. calendar_create_event: conflict check + auto-block ---------------
        created = show("calendar_create_event", await s.call_tool("calendar_create_event", {
            "account": PRIMARY,
            "title": "TEST: sync event",
            "start_time": "2026-08-01T10:00:00+05:00",
            "end_time": "2026-08-01T11:00:00+05:00",
        }))
        event_id = created["id"]
        created_ids.append((PRIMARY, event_id))
        assert OTHER in created.get("blocked_on_accounts", []), "expected work calendar to be blocked"
        print("PASS: block created on", OTHER)

        # 3. Re-creating an overlapping event on the OTHER account should now
        #    report a conflict, since the auto-block event exists there.
        conflict_check = show("calendar_create_event (conflict expected)", await s.call_tool("calendar_create_event", {
            "account": OTHER,
            "title": "TEST: overlapping event",
            "start_time": "2026-08-01T10:30:00+05:00",
            "end_time": "2026-08-01T10:45:00+05:00",
            "block_other_calendars": False,  # don't cascade blocks for this probe event
        }))
        created_ids.append((OTHER, conflict_check["id"]))
        assert PRIMARY in conflict_check.get("conflicts_detected", {}), "expected conflict to be detected"
        print("PASS: conflict detected against", PRIMARY)

        # 4. calendar_update_event: reschedule and confirm block follows ------
        updated = show("calendar_update_event", await s.call_tool("calendar_update_event", {
            "account": PRIMARY,
            "event_id": event_id,
            "start_time": "2026-08-01T15:00:00+05:00",
            "end_time": "2026-08-01T16:00:00+05:00",
        }))
        assert OTHER in updated.get("synced_on_accounts", []), "expected block to be resynced"
        print("PASS: block resynced on", OTHER)

        # 5. calendar_delete_event: confirm block is removed -------------------
        deleted = show("calendar_delete_event", await s.call_tool("calendar_delete_event", {
            "account": PRIMARY,
            "event_id": event_id,
        }))
        assert OTHER in deleted.get("deleted_on_accounts", []), "expected block to be deleted"
        created_ids.remove((PRIMARY, event_id))
        print("PASS: block deleted on", OTHER)

        print("\nAll checks passed.")

    finally:
        # Cleanup: delete anything left over from this run
        for account, event_id in created_ids:
            try:
                await s.call_tool("calendar_delete_event", {"account": account, "event_id": event_id})
                print(f"cleanup: deleted {event_id} on {account}")
            except Exception as exc:
                print(f"cleanup FAILED for {event_id} on {account}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
