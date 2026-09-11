"""#935: the Delivery history channel filter must match stored rows.

ChannelType is a (str, Enum), so str(member) is "ChannelType.TOAST" while the
deliveries table stores "toast". These pass a real enum member, which is what
FastAPI hands the repository — a plain-string stub passes even when broken.
"""
from __future__ import annotations

import pytest

from backend.models.notification import ChannelType
from backend.storage.repositories import NotificationRepository


class _RecordingSettingsDB:
    """Captures the channel_type the repository binds into the query."""

    def __init__(self) -> None:
        self.seen: list = []

    def query_deliveries(self, limit=100, offset=0, channel_type=None):
        self.seen.append(channel_type)
        return []


def _repo(db):
    repo = NotificationRepository.__new__(NotificationRepository)
    repo.settings_db = db
    return repo


@pytest.mark.asyncio
async def test_enum_member_filter_binds_the_plain_value():
    db = _RecordingSettingsDB()
    await _repo(db).get_delivery_history(channel_type=ChannelType.TOAST)
    assert db.seen == ["toast"]


@pytest.mark.asyncio
async def test_every_channel_binds_its_own_value():
    db = _RecordingSettingsDB()
    repo = _repo(db)
    for ct in ChannelType:
        await repo.get_delivery_history(channel_type=ct)
    assert db.seen == [c.value for c in ChannelType]


@pytest.mark.asyncio
async def test_plain_string_filter_still_works():
    db = _RecordingSettingsDB()
    await _repo(db).get_delivery_history(channel_type="discord")
    assert db.seen == ["discord"]


@pytest.mark.asyncio
async def test_all_channels_passes_no_filter():
    db = _RecordingSettingsDB()
    await _repo(db).get_delivery_history(channel_type=None)
    assert db.seen == [None]
