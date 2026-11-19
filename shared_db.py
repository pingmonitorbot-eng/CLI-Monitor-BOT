"""Shared Mongo helpers for Controller + Worker."""
import os
from typing import List, Optional
from datetime import datetime, timezone
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "sms_monitor")

_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = _client[MONGO_DB]
col_panels = db["panels"]
col_settings = db["settings"]
col_cli_state = db["cli_state"]  # latest event time per panel+cli


def ping() -> bool:
    _client.admin.command("ping")
    return True


def get_settings() -> dict:
    doc = col_settings.find_one({"_id": "main"}) or {}
    return {
        "group_chat_id": doc.get("group_chat_id"),
        "monitor_running": doc.get("monitor_running", False),
        "clis": doc.get("clis") or [],
        "worker_heartbeat": doc.get("worker_heartbeat"),
        "worker_run_id": doc.get("worker_run_id"),
        "worker_started_at": doc.get("worker_started_at"),
        "last_cycle_at": doc.get("last_cycle_at"),
        "last_cycle_stats": doc.get("last_cycle_stats"),
    }


def save_settings(**kwargs):
    col_settings.update_one({"_id": "main"}, {"$set": kwargs}, upsert=True)


def list_panels() -> List[dict]:
    return list(col_panels.find().sort("name", 1))


def get_panel(panel_id) -> Optional[dict]:
    from bson import ObjectId
    try:
        return col_panels.find_one({"_id": ObjectId(panel_id)})
    except Exception:
        return None


def get_cli_latest_time(panel_id: str, cli: str) -> Optional[str]:
    """ISO time string of last posted/seen event for panel+cli, or None."""
    doc = col_cli_state.find_one({"_id": f"{panel_id}::{cli}"})
    if not doc:
        return None
    return doc.get("latest_time") or None


def set_cli_latest_time(panel_id: str, cli: str, latest_time: str):
    col_cli_state.update_one(
        {"_id": f"{panel_id}::{cli}"},
        {
            "$set": {
                "panel_id": panel_id,
                "cli": cli,
                "latest_time": latest_time,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )
