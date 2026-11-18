"""Shared Mongo helpers for Controller + Worker."""
import os
from typing import List, Optional, Any
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "sms_monitor")

_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = _client[MONGO_DB]
col_panels = db["panels"]
col_settings = db["settings"]


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
