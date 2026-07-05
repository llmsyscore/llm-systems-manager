"""REST API routes for alarm rule CRUD operations.

Endpoints:
    GET    /api/alarm/rules          - List all rules (with optional filters)
    POST   /api/alarm/rules          - Create a new rule
    GET    /api/alarm/rules/{id}     - Get rule by ID
    PUT    /api/alarm/rules/{id}     - Update rule
    DELETE /api/alarm/rules/{id}     - Delete rule
    PATCH  /api/alarm/rules/{id}/toggle - Enable/disable rule
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_management_token
from ...models.alarm_rule import (
    AlarmRuleCreate,
    AlarmRuleUpdate,
    RuleType,
)
from ...storage.repositories import RuleRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alarm/rules", tags=["Rules"],
                   dependencies=[Depends(require_management_token)])

# Global dependency map — populated in alarm_engine.py on app startup
_dependency_map: dict = {}


def set_dependencies(deps: dict) -> None:
    """Inject shared dependencies into the routes module."""
    global _dependency_map
    _dependency_map = deps


def _get_repo() -> RuleRepository:
    repo = _dependency_map.get("rule_repository")
    if repo is None:
        raise RuntimeError("RuleRepository not initialized. Call set_dependencies() first.")
    return repo


@router.get("")
async def list_rules(
    enabled: Optional[bool] = None,
    metric_source: Optional[str] = None,
    rule_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List rules with optional filters."""
    repo = _get_repo()
    rules = repo.get_all(enabled_only=False)

    filtered = []
    for r in rules:
        rule_dict = r.to_dict()
        if enabled is not None and r.enabled != enabled:
            continue
        if metric_source is not None and r.metric_source != metric_source:
            continue
        if rule_type is not None and r.rule_type != rule_type:
            continue
        filtered.append(rule_dict)

    # Paginate
    return filtered[offset : offset + limit]


@router.post("")
async def create_rule(rule_create: AlarmRuleCreate) -> dict:
    """Create a new alarm rule."""
    # Validate rule_type
    try:
        RuleType(rule_create.rule_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid rule_type: {rule_create.rule_type}")

    repo = _get_repo()
    rule = repo.create(rule_create)
    logger.info(
        "rule created: id=%s name=%s type=%s metric=%s/%s severity=%s enabled=%s",
        rule.rule_id, rule.name, rule.rule_type,
        rule.metric_source, rule.metric_name,
        rule.severity, rule.enabled,
    )
    return rule.to_dict()


@router.get("/{rule_id}")
async def get_rule(rule_id: str) -> dict:
    """Get a rule by ID."""
    try:
        uid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {rule_id}")

    repo = _get_repo()
    rule = repo.get_by_id(uid)

    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")

    return rule.to_dict()


@router.put("/{rule_id}")
async def update_rule(rule_id: str, rule_update: AlarmRuleUpdate) -> dict:
    """Update an existing rule."""
    try:
        uid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {rule_id}")

    repo = _get_repo()
    existing = repo.get_by_id(uid)

    if not existing:
        raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")

    updated = repo.update(uid, rule_update)
    if not updated:
        logger.error("rule update failed: id=%s", rule_id)
        raise HTTPException(status_code=500, detail="Failed to update rule")

    logger.info("rule updated: id=%s name=%s enabled=%s", updated.rule_id, updated.name, updated.enabled)
    return updated.to_dict()


@router.delete("")
async def delete_all_rules() -> dict:
    """Admin: delete every rule. Used to recover from legacy duplicate rows
    that came from the old InfluxDB schema where `enabled` was a tag."""
    repo = _get_repo()
    if not repo.delete_all():
        raise HTTPException(status_code=500, detail="Failed to delete rules")
    logger.warning("ALL rules deleted via DELETE /api/alarm/rules")
    return {"message": "All rules deleted"}


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str) -> dict:
    """Delete a rule."""
    try:
        uid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {rule_id}")

    repo = _get_repo()
    deleted = repo.delete(uid)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")

    logger.info("rule deleted: id=%s", rule_id)
    return {"message": f"Rule {rule_id} deleted"}


@router.patch("/{rule_id}/toggle")
async def toggle_rule(rule_id: str) -> dict:
    """Toggle rule enabled/disabled status."""
    try:
        uid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {rule_id}")

    repo = _get_repo()
    existing = repo.get_by_id(uid)

    if not existing:
        raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")

    update = AlarmRuleUpdate(enabled=not existing.enabled)
    updated = repo.update(uid, update)

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to toggle rule")

    new_state = updated.enabled
    logger.info("rule toggled: id=%s name=%s enabled=%s", rule_id, updated.name, new_state)
    return {
        "rule_id": str(uid),
        "enabled": new_state,
        "message": f"Rule {'enabled' if new_state else 'disabled'}",
    }