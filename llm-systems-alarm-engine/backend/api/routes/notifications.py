"""
Notification configuration and test API routes.
Provides CRUD for notification channels and test/send capabilities.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...models.notification import (
    NotificationChannel,
    NotificationChannelCreate,
    NotificationChannelType,
    NotificationChannelUpdate,
    NotificationConfig,
    NotificationConfigCreate,
    NotificationConfigUpdate,
    NotificationDelivery,
)
from ...storage.repositories import ConfigDeserializationError, NotificationRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alarm/notifications", tags=["Notifications"])


# ── Request bodies ──────────────────────────────────────────────

class TestPayload(BaseModel):
    """Payload for testing a notification channel."""
    channel_id: Optional[str] = Field(None, description="Channel ID to test")
    channel_type: Optional[NotificationChannelType] = Field(
        None, description="Channel type (if no channel_id)"
    )
    recipient: Optional[str] = Field(None, description="Recipient address")
    title: str = Field("Test Alert", description="Notification title")
    body: str = Field("This is a test notification from the alarm engine.", description="Notification body")
    severity: Optional[str] = "info"


class SendPayload(BaseModel):
    """Payload for sending a notification via a configured channel."""
    config_id: Optional[str] = Field(None, description="Config ID to use")
    channel_id: Optional[str] = Field(None, description="Channel ID to use directly")
    title: str
    body: str
    severity: str = "info"
    metadata: Optional[dict] = Field(default_factory=dict)


# ── Dependency injection ────────────────────────────────────────

_repo: Optional[NotificationRepository] = None
_ws_send = None  # injected after ws_manager starts


def set_repository(repo: NotificationRepository) -> None:
    global _repo
    _repo = repo


def set_ws_send(ws_send_fn) -> None:
    """Inject the WebSocket broadcast callable so test toasts reach the browser."""
    global _ws_send
    _ws_send = ws_send_fn


def _get_repo() -> NotificationRepository:
    if _repo is None:
        raise RuntimeError("NotificationRepository not initialized.")
    return _repo


# ── Channels ────────────────────────────────────────────────────

@router.get("/channels", response_model=List[NotificationChannel])
async def list_channels() -> List[NotificationChannel]:
    """List all notification channels."""
    repo = _get_repo()
    return await repo.list_channels()


@router.post("/channels", response_model=NotificationChannel, status_code=201)
async def create_channel(payload: NotificationChannelCreate) -> NotificationChannel:
    """Create a new notification channel."""
    repo = _get_repo()
    ch = repo.create(payload)
    logger.info("notification channel created: id=%s name=%s type=%s",
                getattr(ch, "channel_id", None), getattr(ch, "name", None),
                getattr(ch, "type", None))
    return ch


@router.get("/channels/{channel_id}", response_model=NotificationChannel)
async def get_channel(channel_id: str) -> NotificationChannel:
    """Get a channel by ID."""
    repo = _get_repo()
    channel = await repo.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
    return channel


@router.put("/channels/{channel_id}", response_model=NotificationChannel)
async def update_channel(channel_id: str, payload: NotificationChannelUpdate) -> NotificationChannel:
    """Update a channel."""
    repo = _get_repo()
    ch = await repo.update_channel(channel_id, payload)
    logger.info("notification channel updated: id=%s", channel_id)
    return ch


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str) -> dict:
    """Delete a channel."""
    repo = _get_repo()
    success = await repo.delete_channel(channel_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
    logger.info("notification channel deleted: id=%s", channel_id)
    return {"status": "ok", "message": f"Channel {channel_id} deleted"}


# ── Configs ─────────────────────────────────────────────────────

@router.get("/configs", response_model=List[NotificationConfig])
async def list_configs() -> List[NotificationConfig]:
    """List all notification configs."""
    repo = _get_repo()
    return repo.list_configs()


@router.post("/configs", response_model=NotificationConfig, status_code=201)
async def create_config(payload: NotificationConfigCreate) -> NotificationConfig:
    """Create a new notification config."""
    repo = _get_repo()
    return repo.create_config(payload)


@router.get("/configs/{config_id}", response_model=NotificationConfig)
async def get_config(config_id: str) -> NotificationConfig:
    """Get a config by ID."""
    import uuid as _uuid
    repo = _get_repo()
    try:
        cfg_uuid = _uuid.UUID(config_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid config_id: {config_id}")
    try:
        config = repo.get_config(cfg_uuid)
    except ConfigDeserializationError as e:
        raise HTTPException(status_code=422, detail=f"Config {config_id} is stored but could not be parsed: {e}")
    if not config:
        raise HTTPException(status_code=404, detail=f"Config {config_id} not found")
    return config


@router.put("/configs/{config_id}", response_model=NotificationConfig)
async def update_config(config_id: str, payload: NotificationConfigUpdate) -> NotificationConfig:
    """Update a config."""
    import uuid as _uuid
    repo = _get_repo()
    try:
        cfg_uuid = _uuid.UUID(config_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid config_id: {config_id}")
    try:
        updated = repo.update_config(cfg_uuid, payload)
    except ConfigDeserializationError as e:
        raise HTTPException(status_code=422, detail=f"Config {config_id} update produced an invalid model: {e}")
    if not updated:
        raise HTTPException(status_code=404, detail=f"Config {config_id} not found")
    return updated


@router.delete("/configs/{config_id}")
async def delete_config(config_id: str) -> dict:
    """Delete a config."""
    import uuid as _uuid
    repo = _get_repo()
    try:
        cfg_uuid = _uuid.UUID(config_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid config_id: {config_id}")
    success = repo.delete_config(cfg_uuid)
    if not success:
        raise HTTPException(status_code=404, detail=f"Config {config_id} not found")
    return {"status": "ok", "message": f"Config {config_id} deleted"}


# ── Delivery History ────────────────────────────────────────────

@router.get("/delivery-history", response_model=List[NotificationDelivery])
async def get_delivery_history(
    limit: int = Query(100, ge=1, le=1000),
    channel_type: Optional[NotificationChannelType] = None,
) -> List[NotificationDelivery]:
    """Get notification delivery history."""
    repo = _get_repo()
    return await repo.get_delivery_history(limit=limit, channel_type=channel_type)


# ── Send / Test ─────────────────────────────────────────────────

@router.post("/send")
async def send_notification(payload: SendPayload) -> dict:
    """Send a notification using the specified config or channel."""
    repo = _get_repo()
    result = await repo.send_notification(
        title=payload.title,
        body=payload.body,
        severity=payload.severity,
        config_id=payload.config_id,
        channel_id=payload.channel_id,
        metadata=payload.metadata,
    )
    if not result.get("success"):
        logger.warning("notification send failed: config=%s channel=%s reason=%s",
                       payload.config_id, payload.channel_id, result.get("error"))
        raise HTTPException(status_code=500, detail=result.get("error", "Send failed"))
    logger.info("notification sent: config=%s channel=%s severity=%s title=%r",
                payload.config_id, payload.channel_id, payload.severity, payload.title)
    return {"status": "ok", "message": "Notification sent", "details": result}


@router.post("/test")
async def test_channel(payload: TestPayload) -> dict:
    """Test a notification channel.

    If channel_id is provided, the saved channel's configuration is loaded so
    the test exercises the real recipient (email address, webhook URL, etc.)
    rather than dispatcher defaults.
    """
    from ...engine.notification_dispatcher import NotificationDispatcher

    channel_type = payload.channel_type
    recipient = payload.recipient
    resolved_from_saved_channel = False

    if payload.channel_id:
        try:
            repo = _get_repo()
            channel = await repo.get_channel(payload.channel_id)
        except Exception as e:
            logger.warning("Failed to load channel %s for test: %s", payload.channel_id, e)
            channel = None
        if channel:
            channel_type = channel.channel_type
            cfg = channel.config
            if channel_type == NotificationChannelType.EMAIL and cfg.email:
                recipient = cfg.email.to_email
            elif channel_type == NotificationChannelType.SMS and cfg.sms:
                recipient = cfg.sms.to_number
            elif channel_type == NotificationChannelType.WEBHOOK and cfg.webhook:
                recipient = cfg.webhook.url
            elif channel_type == NotificationChannelType.DISCORD and cfg.discord:
                recipient = cfg.discord.webhook_url
            resolved_from_saved_channel = True

    # Reject test dispatches whose URL-bearing recipient comes from the request
    # body. Without this, /test is an open SSRF: an attacker could supply
    # channel_type=webhook|discord plus any URL and the dispatcher would POST
    # to it from the alarm engine host, reaching localhost / RFC1918 services.
    if channel_type in (NotificationChannelType.WEBHOOK, NotificationChannelType.DISCORD) \
            and not resolved_from_saved_channel:
        raise HTTPException(
            status_code=400,
            detail="Testing webhook/discord channels requires a saved channel_id; "
                   "free-form recipient URLs are not accepted.",
        )

    dispatcher = NotificationDispatcher(websocket_send=_ws_send)
    result = await dispatcher.send_notification(
        channel_type=channel_type or NotificationChannelType.TOAST,
        recipient=recipient or "local",
        title=payload.title,
        body=payload.body,
        severity=payload.severity,
        metadata={"test": True},
    )

    # Record the delivery so it appears in history
    repo = _get_repo()
    repo.record_delivery(
        channel_id=payload.channel_id,
        channel_type=str((channel_type or NotificationChannelType.TOAST).value),
        title=payload.title,
        body=payload.body,
        severity=payload.severity or "info",
        recipient=recipient or "local",
        success=result.get("success", False),
        error_message=result.get("error"),
    )

    return {"status": "ok" if result.get("success") else "error", "details": result}