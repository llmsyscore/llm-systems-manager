"""Delivery rows carry the alert id in metadata and bump the channel's
send / fail counters (#810 alert drawer timeline, channel ledger)."""
import uuid

from backend._time import now_utc
from backend.models.notification import (ChannelSpecificConfig, ChannelType,
                                         NotificationChannel, ToastConfig)
from backend.storage.ae_settings_db import AeSettingsDB
from backend.storage.cache import MetricCache
from backend.storage.repositories import NotificationRepository


def _repo(tmp_path):
    db = AeSettingsDB.open(tmp_path / "settings.db")
    return NotificationRepository(cache=MetricCache(), settings_db=db), db


def _toast_channel():
    return NotificationChannel(
        channel_id=uuid.uuid4(), name="Toast", description=None,
        channel_type=ChannelType.TOAST,
        config=ChannelSpecificConfig(toast=ToastConfig()),
        enabled=True, rule_ids=[], created_at=now_utc(), last_sent_at=None,
        send_count=0, fail_count=0)


async def test_metadata_round_trips_through_sqlite(tmp_path):
    repo, _ = _repo(tmp_path)
    repo.record_delivery(channel_id=None, channel_type="toast", title="t", body="b",
                         severity="info", recipient="webui", success=True,
                         metadata={"alert_id": "abc"})
    rows = await repo.get_delivery_history(limit=5)
    assert len(rows) == 1
    assert rows[0].metadata == {"alert_id": "abc"}


async def test_success_and_failure_bump_the_channel_counters(tmp_path):
    repo, db = _repo(tmp_path)
    ch = _toast_channel()
    db.write_channel(ch.to_dict())
    cid = str(ch.channel_id)
    repo.record_delivery(channel_id=cid, channel_type="toast", title="t", body="b",
                         severity="info", recipient="webui", success=True)
    repo.record_delivery(channel_id=cid, channel_type="toast", title="t", body="b",
                         severity="info", recipient="webui", success=False,
                         error_message="socket closed")
    after = [c for c in await repo.list_channels() if str(c.channel_id) == cid][0]
    assert after.send_count == 1
    assert after.fail_count == 1
    assert after.last_sent_at is not None


def test_old_tables_gain_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = AeSettingsDB.open(path)
    db._conn.execute("ALTER TABLE deliveries DROP COLUMN metadata_json")
    db._conn.execute("ALTER TABLE configs DROP COLUMN toast_dismiss_seconds")
    db._conn.commit()
    db.close()
    db = AeSettingsDB.open(path)
    assert "metadata_json" in {r[1] for r in db._conn.execute("PRAGMA table_info(deliveries)")}
    assert "toast_dismiss_seconds" in {r[1] for r in db._conn.execute("PRAGMA table_info(configs)")}
    db.close()


def test_toast_dismiss_seconds_round_trips(tmp_path):
    from backend.models.notification import NotificationConfigCreate
    repo, _ = _repo(tmp_path)
    cfg = repo.create_config(NotificationConfigCreate(name="p", channels=[], toast_dismiss_seconds=45))
    stored = repo.list_configs()
    assert [c.toast_dismiss_seconds for c in stored if c.config_id == cfg.config_id] == [45]
