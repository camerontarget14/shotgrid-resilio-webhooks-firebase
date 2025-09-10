"""• Resilio webhooks – Firebase Cloud Functions
----------------------------------------------------------------
Exports HTTP Cloud Functions for ShotGrid webhook integration with Resilio state sync.

- assignment_webhook
- shot_status_webhook     – Shot status change (triggers full sync)

Deploy (Gen‑2):
    firebase deploy --only functions
"""
from __future__ import annotations
from resilio_state_sync import ResilioStateSyncManager, ShotGridStateManager
import os, json, hmac, hashlib, logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import shotgun_api3
import functions_framework            # local dev convenience
from firebase_functions import https_fn  # GCF/Firebase runtime
from flask import Request, abort, make_response, jsonify

# ─────────────────────────────── Standard Python Logging ────────────────────────
# Set up a logger with a name in Firebase Functions
logger = logging.getLogger("resilio-webhooks")

# Configure the logger
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Configure the handler (Firebase Functions automatically captures stdout/stderr)
handler = logging.StreamHandler()
handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(handler)

# Set the logging level
logger.setLevel(logging.INFO)

# ─────────────────────────────── Configuration ──────────────────────────────
ROOT = os.path.dirname(__file__)
with open(os.path.join(ROOT, "config.json"), "rt", encoding="utf8") as f:
    _CONF = json.load(f)

SG_HOST        = _CONF["SHOTGRID_URL"]
SG_API_KEY     = _CONF["SHOTGRID_API_KEY"]
SG_SCRIPT_NAME = _CONF["SHOTGRID_SCRIPT_NAME"]
SECRET_TOKEN   = _CONF["SECRET_TOKEN"].encode()
RESILIO_URL = _CONF.get("RESILIO_URL", "")
RESILIO_TOKEN = _CONF.get("RESILIO_TOKEN", "")

logger.info("Starting ShotGrid webhooks service with Resilio state sync")
logger.info(f"Using ShotGrid host: {SG_HOST}")
logger.info(f"Using script name: {SG_SCRIPT_NAME}")

# ─────────────────────────────── Singleton SG client ────────────────────────
logger.info("Initializing ShotGrid client connection")
try:
    _SG_CLIENT = shotgun_api3.Shotgun(
        SG_HOST,
        script_name=SG_SCRIPT_NAME,
        api_key=SG_API_KEY,
        connect=True,
    )
    logger.info("ShotGrid client connection successful")
except Exception as e:
    logger.error(f"Failed to initialize ShotGrid client: {str(e)}")
    raise

# ─────────────────────────────── ShotGrid helper ────────────────────────────
class SG:
    """Lightweight wrapper re‑using one persistent ShotGrid session."""
    def __init__(self):
        self._sg = _SG_CLIENT

    # Queries
    def find_task(self, tid: int):
        logger.info(f"Finding Task {tid}")
        try:
            result = self._sg.find_one(
                "Task", [["id", "is", tid]],
                ["id", "step", "sg_status_list", "entity", "project", "task_assignees"],
            )
            if result:
                step_name = (result.get("step") or {}).get("name")
                assignees = result.get("task_assignees", [])
                logger.info(f"Found Task {tid} with status {result.get('sg_status_list')} and step {step_name}, {len(assignees)} assignees")
            else:
                logger.warning(f"Task {tid} not found")
            return result
        except Exception as e:
            logger.error(f"Error finding Task {tid}: {str(e)}")
            return None

    def find_shot(self, sid: int):
        logger.info(f"Finding Shot {sid}")
        try:
            result = self._sg.find_one(
                "Shot", [["id", "is", sid]], ["id", "sg_status_list", "code", "project"],
            )
            if result:
                project_name = (result.get("project") or {}).get("name", "")
                logger.info(f"Found Shot {sid} ({result.get('code')}) in project '{project_name}' with status {result.get('sg_status_list')}")
            else:
                logger.warning(f"Shot {sid} not found")
            return result
        except Exception as e:
            logger.error(f"Error finding Shot {sid}: {str(e)}")
            return None

# ─────────────────────────────── Helper utils ───────────────────────────────

def _verify_sig(body: bytes, sig: Optional[str]) -> bool:
    logger.info("Verifying webhook signature")
    if not sig:
        logger.warning("No signature provided in request")
        return False
    sig = sig[5:] if sig.startswith("sha1=") else sig
    expected = hmac.new(SECRET_TOKEN, body, hashlib.sha1).hexdigest()
    result = hmac.compare_digest(expected, sig)
    if result:
        logger.info("Signature verification successful")
    else:
        logger.warning("Signature verification failed")
    return result

def _entity_id(data: dict) -> Optional[int]:
    logger.info("Extracting entity ID from payload")
    entity_id = None
    if "entity_id" in data:
        entity_id = data["entity_id"]
        logger.info(f"Found entity_id: {entity_id}")
    else:
        ent = data.get("entity")
        if isinstance(ent, dict):
            entity_id = ent.get("id")
            logger.info(f"Found entity.id: {entity_id}")

    if entity_id is None:
        logger.warning("No entity ID found in payload")

    return entity_id

# ─────────────────────────────── Handlers ───────────────────────────────────

def _handle_task_assignment(payload: dict):
    """Handle new task assignment - triggers shot status sync."""
    logger.info("Task assignment webhook triggered - triggering shot status sync")
    logger.debug(f"Task assignment payload: {json.dumps(payload)}")

    try:
        # Extract task information from payload
        task_id = _entity_id(payload["data"])

        if not task_id:
            logger.error("Failed to extract task ID from payload")
            return {"error": "No task ID found"}

        # Get task details from ShotGrid
        sg = SG()
        task = sg.find_task(task_id)

        if not task:
            logger.error(f"Task {task_id} not found in ShotGrid")
            return {"error": f"Task {task_id} not found"}

        # Get shot information
        entity = task.get("entity")
        if not entity or entity.get("type") != "Shot":
            logger.info(f"Task {task_id} is not linked to a Shot, skipping")
            return {"message": "Task not linked to Shot", "task_id": task_id}

        shot_id = entity.get("id")
        shot = sg.find_shot(shot_id) if shot_id else None

        if not shot:
            logger.error(f"Shot {shot_id} not found")
            return {"error": f"Shot {shot_id} not found"}

        shot_name = shot.get("code", "")
        shot_status = shot.get("sg_status_list", "")

        # If shot is active, trigger full sync (same as shot status webhook)
        if shot_status == "active":
            logger.info(f"Shot {shot_name} is active, triggering full Resilio sync")

            # Validate Resilio configuration
            if not RESILIO_URL or not RESILIO_TOKEN:
                logger.error("Resilio Connect credentials not configured")
                return {"error": "Resilio Connect not configured"}

            # Initialize managers
            sg_state_manager = ShotGridStateManager(_SG_CLIENT)
            resilio_sync_manager = ResilioStateSyncManager()

            # Get current ShotGrid state
            sg_state = sg_state_manager.get_active_shots_with_assignments()

            # Sync Resilio to match ShotGrid state
            sync_results = resilio_sync_manager.sync_resilio_to_shotgrid_state(
                sg_state=sg_state,
                resilio_url=RESILIO_URL,
                resilio_token=RESILIO_TOKEN
            )

            logger.info(f"Assignment sync complete: {sync_results['shot_jobs_created']} shot jobs created, "
                       f"{sync_results['shot_jobs_updated']} updated")

            return {
                "task_id": task_id,
                "shot_name": shot_name,
                "shot_status": shot_status,
                "trigger_reason": "assignment_to_active_shot",
                "sync_results": sync_results
            }
        else:
            logger.info(f"Shot {shot_name} status is '{shot_status}', not active - no sync needed")
            return {
                "task_id": task_id,
                "shot_name": shot_name,
                "shot_status": shot_status,
                "message": "Shot not active, no sync performed"
            }

    except Exception as e:
        logger.error(f"Task assignment webhook failed: {e}")
        return {"error": f"Webhook processing failed: {str(e)}"}

def _handle_shot_status(payload: dict):
    """Handle shot status changes and sync Resilio state."""
    logger.info("Shot status webhook triggered - starting full Resilio sync")
    logger.debug(f"Shot status payload: {json.dumps(payload)}")

    meta = payload["data"].get("meta", {})
    attribute_name = meta.get("attribute_name")

    if attribute_name != "sg_status_list":
        logger.info(f"Ignoring update to attribute '{attribute_name}', only handling sg_status_list")
        return {"ignored": True, "reason": f"attribute_name is '{attribute_name}', not 'sg_status_list'"}

    shot_id = _entity_id(payload["data"])
    if shot_id is None:
        logger.error("Failed to extract shot ID from payload")
        return {"error": "No shot ID"}

    new_status = meta.get("new_value")
    old_status = meta.get("old_value")
    logger.info(f"Shot {shot_id} status changed from '{old_status}' to '{new_status}'")

    try:
        # Validate Resilio configuration
        if not RESILIO_URL or not RESILIO_TOKEN:
            logger.error("Resilio Connect credentials not configured")
            return {"error": "Resilio Connect not configured"}

        # Initialize managers
        sg_state_manager = ShotGridStateManager(_SG_CLIENT)
        resilio_sync_manager = ResilioStateSyncManager()

        # Get current ShotGrid state
        logger.info("Querying current ShotGrid state...")
        sg_state = sg_state_manager.get_active_shots_with_assignments()

        active_shots_count = len(sg_state['shots'])
        artists_count = len(sg_state['artist_projects'])
        logger.info(f"Found {active_shots_count} active shots across {artists_count} artists")

        # Sync Resilio to match ShotGrid state
        logger.info("Synchronizing Resilio jobs to match ShotGrid state...")
        sync_results = resilio_sync_manager.sync_resilio_to_shotgrid_state(
            sg_state=sg_state,
            resilio_url=RESILIO_URL,
            resilio_token=RESILIO_TOKEN
        )

        # Log summary
        logger.info(f"Sync complete: {sync_results['shot_jobs_created']} shot jobs created, "
                   f"{sync_results['shot_jobs_updated']} updated, "
                   f"{sync_results['shot_jobs_hydrated']} hydrated, "
                   f"{sync_results['assets_jobs_created']} assets jobs created, "
                   f"{sync_results['assets_jobs_updated']} assets updated")

        if sync_results['errors']:
            logger.warning(f"Sync completed with {len(sync_results['errors'])} errors")
            for error in sync_results['errors']:
                logger.warning(f"  - {error}")

        return {
            "trigger_shot_id": shot_id,
            "trigger_status_change": f"{old_status} -> {new_status}",
            "active_shots_found": active_shots_count,
            "artists_found": artists_count,
            "sync_results": sync_results
        }

    except Exception as e:
        logger.error(f"Shot status webhook failed: {e}")
        return {"error": f"Sync processing failed: {str(e)}"}

# ─────────────────────────────── Dispatcher ────────────────────────────────

def _dispatch(request: Request, route: Optional[str] = None):
    path = request.path
    key = (route or path.rstrip("/").split("/")[-1]).lower()
    logger.info(f"Received webhook request to path '{path}', dispatching as '{key}'")

    body_data = request.get_data()
    logger.debug(f"Request body size: {len(body_data)} bytes")

    sig = request.headers.get("X-SG-Signature")
    if not _verify_sig(body_data, sig):
        logger.warning(f"Unauthorized request to {path}: Invalid signature")
        abort(make_response(("Unauthorized", 401)))

    try:
        payload = request.get_json(force=True)
        logger.debug(f"Parsed JSON payload type: {payload.get('event_type', 'unknown')}")
    except Exception as e:
        logger.error(f"Failed to parse JSON from request: {str(e)}")
        abort(make_response(("Bad JSON", 400)))

    if key == "assignment":
        logger.info("Handling as assignment webhook")
        result = _handle_task_assignment(payload)
    elif key in {"shot", "shot_status", "shot-status"}:
        logger.info("Handling as shot status webhook")
        result = _handle_shot_status(payload)
    else:
        logger.warning(f"Unknown webhook type: {key}")
        abort(make_response(("Not Found", 404)))

    ts = payload.get("timestamp")
    if ts:
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            lag_ms = int((datetime.now(timezone.utc) - ts_dt).total_seconds()*1000)
            result["lag_ms"] = lag_ms
            logger.info(f"Event processing lag: {lag_ms}ms")
        except Exception as e:
            logger.warning(f"Bad timestamp '{ts}': {str(e)}")

    logger.info(f"Webhook {key} processing complete: {json.dumps(result)}")
    response = jsonify(result)
    return response

# ─────────────────────────────── Cloud Function exports ────────────────────

# Local testing entrypoint
@functions_framework.http
def main(request: Request):
    logger.info("main function called (local development)")
    return _dispatch(request)

# Resilio endpoint
@https_fn.on_request()
def assignment_webhook(request: Request):
    """HTTP Cloud Function for task assignment webhooks."""
    return _dispatch(request, "assignment")

@https_fn.on_request()
def shot_status_webhook(request: Request):
    """HTTP Cloud Function for shot status webhooks."""
    return _dispatch(request, "shot_status")
