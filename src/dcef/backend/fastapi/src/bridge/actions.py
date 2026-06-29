import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..common.Database import db


router = APIRouter(prefix="", tags=["bridge-actions"])

DEFAULT_DISCORD_API_BASE = "https://discord.com/api/v10"
MATRIX_ROOM_ID_RE = re.compile(r"^![^:\s]+:[^:\s]+$")
MATRIX_EVENT_ID_RE = re.compile(r"^\$[^\s]+$")
DISCORD_ID_RE = re.compile(r"^[0-9]{1,32}$")
SUPPORTED_DIRECTIONS = {"m2d", "d2m"}
SUPPORTED_ACTIONS = {"send", "edit", "delete", "reaction", "remove_reaction"}


def utc_now() -> str:
	return datetime.now(timezone.utc).isoformat()


def env_first(keys: tuple[str, ...]) -> str | None:
	for key in keys:
		value = os.getenv(key)
		if value:
			return value
	return None


def config_value(*keys: str) -> str | None:
	for key in keys:
		doc = db["config"].find_one({"key": key})
		value = doc.get("value") if isinstance(doc, dict) else None
		if isinstance(value, str) and value:
			return value
		if isinstance(value, dict):
			for nested_key in ("token", "url", "access_token", "bot_token", "homeserver_url"):
				nested = value.get(nested_key)
				if isinstance(nested, str) and nested:
					return nested
	return None


def discord_token(payload: dict) -> str | None:
	return (
		payload.get("discord_token")
		or config_value("discord_bot_token", "discord_token", "archive_discord_token")
		or env_first(("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"))
	)


def discord_api_base(payload: dict) -> str:
	value = payload.get("discord_api_base") or config_value("discord_api_base") or env_first(("DISCORD_API_BASE",))
	if isinstance(value, str) and value:
		return value.rstrip("/")
	return DEFAULT_DISCORD_API_BASE


def matrix_access_token(payload: dict) -> str | None:
	return (
		payload.get("matrix_access_token")
		or config_value("matrix_access_token", "matrix_appservice_as_token")
		or env_first(("MATRIX_ACCESS_TOKEN", "MATRIX_APPSERVICE_AS_TOKEN", "MATRIX_AS_TOKEN"))
	)


def matrix_homeserver_url(payload: dict) -> str | None:
	value = (
		payload.get("matrix_homeserver_url")
		or config_value("matrix_homeserver_url", "matrix_homeserver")
		or env_first(("MATRIX_HOMESERVER_URL", "MATRIX_HOMESERVER"))
	)
	return value.rstrip("/") if isinstance(value, str) and value else None


def action_collection():
	return db["bridge_action_outbox"]


def mappings_collection():
	return db["bridge_message_mappings"]


def clean_payload(payload: dict) -> dict:
	redacted = dict(payload)
	for key in ("discord_token", "matrix_access_token"):
		if key in redacted:
			redacted[key] = "[redacted]"
	return redacted


def stable_action_id(direction: str, action: str, payload: dict) -> str:
	key = payload.get("idempotency_key") or payload.get("txn_id") or payload.get("event_id")
	if key:
		return hashlib.sha256(f"{direction}:{action}:{key}".encode("utf-8")).hexdigest()
	body = json.dumps(clean_payload(payload), sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(f"{direction}:{action}:{body}".encode("utf-8")).hexdigest()


def insert_or_update_action(direction: str, action: str, payload: dict, status: str, planned_request: dict, response: dict | None = None, error: str | None = None):
	_id = stable_action_id(direction, action, payload)
	update = {
		"$set": {
			"direction": direction,
			"action": action,
			"payload": clean_payload(payload),
			"status": status,
			"planned_request": planned_request,
			"updated_at": utc_now(),
		},
		"$setOnInsert": {"created_at": utc_now()},
	}
	if response is not None:
		update["$set"]["response"] = response
	if error is not None:
		update["$set"]["error"] = error
	action_collection().update_one({"_id": _id}, update, upsert=True)
	doc = action_collection().find_one({"_id": _id}, {"payload.discord_token": 0, "payload.matrix_access_token": 0})
	return doc


def validate_direction_action(direction: str, action: str):
	if direction not in SUPPORTED_DIRECTIONS:
		raise HTTPException(status_code=422, detail=f"direction must be one of {sorted(SUPPORTED_DIRECTIONS)}")
	if action not in SUPPORTED_ACTIONS:
		raise HTTPException(status_code=422, detail=f"action must be one of {sorted(SUPPORTED_ACTIONS)}")


def request_json(method: str, url: str, headers: dict, body: dict | None = None):
	data = None if body is None else json.dumps(body).encode("utf-8")
	request = urllib.request.Request(url, data=data, method=method, headers=headers)
	try:
		with urllib.request.urlopen(request, timeout=30) as response:
			raw = response.read().decode("utf-8")
			return {
				"status": response.status,
				"body": json.loads(raw) if raw else None,
			}
	except urllib.error.HTTPError as error:
		raw = error.read().decode("utf-8")
		try:
			body = json.loads(raw) if raw else None
		except json.JSONDecodeError:
			body = raw
		return {"status": error.code, "body": body}


def message_content(payload: dict) -> str:
	content = payload.get("content")
	if isinstance(content, str):
		return content
	event = payload.get("event")
	if isinstance(event, dict):
		event_content = event.get("content") or {}
		new_content = event_content.get("m.new_content") if isinstance(event_content.get("m.new_content"), dict) else None
		return (new_content or event_content).get("body") or ""
	return ""


def reply_to_event_id(payload: dict) -> str | None:
	value = payload.get("reply_to_event_id") or payload.get("target_event_id")
	return str(value) if value else None


def reply_to_discord_message_id(payload: dict) -> str | None:
	value = payload.get("reply_to_message_id") or payload.get("referenced_message_id")
	return str(value) if value else None


def mapped_matrix_target(payload: dict) -> tuple[str | None, str | None]:
	room_id = payload.get("room_id") or payload.get("matrix_room_id")
	event_id = reply_to_event_id(payload) or payload.get("event_id")
	if event_id:
		return room_id, str(event_id)
	discord_message_id = reply_to_discord_message_id(payload) or payload.get("message_id") or payload.get("discord_message_id")
	if discord_message_id:
		mapping = mappings_collection().find_one({"discord_message_id": str(discord_message_id)})
		if mapping:
			return room_id or mapping.get("matrix_room_id"), mapping.get("matrix_event_id")
	return room_id, None


def mapped_discord_reply_message_id(payload: dict) -> str | None:
	message_id = reply_to_discord_message_id(payload)
	if message_id:
		return message_id
	target_event_id = reply_to_event_id(payload)
	if target_event_id:
		mapping = mappings_collection().find_one({"matrix_event_id": target_event_id})
		if mapping:
			return mapping.get("discord_message_id")
	return None


def matrix_event_content(payload: dict, action: str) -> dict:
	if isinstance(payload.get("content"), dict):
		return payload["content"]
	if action == "reaction":
		target = reply_to_event_id(payload)
		if not target or MATRIX_EVENT_ID_RE.match(str(target)) is None:
			raise HTTPException(status_code=422, detail="d2m reaction requires target_event_id")
		return {
			"m.relates_to": {
				"rel_type": "m.annotation",
				"event_id": target,
				"key": payload.get("emoji") or "?",
			}
		}
	if action == "edit":
		target = reply_to_event_id(payload)
		if not target or MATRIX_EVENT_ID_RE.match(str(target)) is None:
			raise HTTPException(status_code=422, detail="d2m edit requires target_event_id")
		content = message_content(payload)
		return {
			"body": f"* {content}",
			"msgtype": "m.text",
			"m.new_content": {"body": content, "msgtype": "m.text"},
			"m.relates_to": {
				"rel_type": "m.replace",
				"event_id": target,
			},
		}
	content = {"body": message_content(payload), "msgtype": payload.get("msgtype") or "m.text"}
	_, target = mapped_matrix_target(payload)
	if action == "send" and reply_to_discord_message_id(payload) and target is None and reply_to_event_id(payload) is None:
		raise HTTPException(status_code=422, detail="d2m send reply requires a mapped Matrix event or explicit reply_to_event_id")
	if action == "send" and target:
		if MATRIX_EVENT_ID_RE.match(str(target)) is None:
			raise HTTPException(status_code=422, detail="d2m send reply requires a valid Matrix target_event_id or mapped Discord message")
		content["m.relates_to"] = {"m.in_reply_to": {"event_id": target}}
	return content


def find_matrix_message_by_event(event_id: str | None):
	if not event_id:
		return None
	guild = "000000000000000000000002"
	return db[f"g{guild}_messages"].find_one({"bridge.event_id": event_id})


def record_mapping(direction: str, action: str, payload: dict, response: dict | None):
	matrix_event_id = payload.get("event_id")
	discord_message_id = payload.get("discord_message_id")
	if action == "send":
		discord_message_id = discord_message_id or payload.get("message_id")
	if response and isinstance(response.get("body"), dict):
		if direction == "m2d":
			discord_message_id = discord_message_id or response["body"].get("id")
		if direction == "d2m":
			matrix_event_id = matrix_event_id or response["body"].get("event_id")
	if not matrix_event_id and not discord_message_id:
		return
	mappings_collection().update_one(
		{"matrix_event_id": matrix_event_id, "discord_message_id": discord_message_id},
		{"$set": {
			"matrix_event_id": matrix_event_id,
			"matrix_room_id": payload.get("room_id"),
			"discord_message_id": discord_message_id,
			"discord_channel_id": payload.get("discord_channel_id") or payload.get("channel_id"),
			"direction": direction,
			"updated_at": utc_now(),
		}, "$setOnInsert": {"created_at": utc_now()}},
		upsert=True,
	)


def mapped_discord_target(payload: dict) -> tuple[str | None, str | None]:
	channel_id = payload.get("discord_channel_id") or payload.get("channel_id")
	message_id = payload.get("discord_message_id") or payload.get("message_id")
	if channel_id and message_id:
		return str(channel_id), str(message_id)
	target_event_id = payload.get("target_event_id") or payload.get("event_id")
	mapping = mappings_collection().find_one({"matrix_event_id": target_event_id}) if target_event_id else None
	if mapping:
		return channel_id or mapping.get("discord_channel_id"), message_id or mapping.get("discord_message_id")
	message = find_matrix_message_by_event(target_event_id)
	discord = ((message or {}).get("bridge") or {}).get("discord") or {}
	return channel_id or discord.get("channel_id"), message_id or discord.get("message_id")


def discord_plan(action: str, payload: dict) -> dict:
	base = discord_api_base(payload)
	channel_id, message_id = mapped_discord_target(payload)
	if action == "send":
		channel_id = payload.get("discord_channel_id") or payload.get("channel_id")
		if not channel_id:
			raise HTTPException(status_code=422, detail="m2d send requires discord_channel_id or channel_id")
		body = {"content": message_content(payload), "allowed_mentions": {"parse": []}}
		reply_message_id = mapped_discord_reply_message_id(payload)
		if (reply_to_event_id(payload) or reply_to_discord_message_id(payload)) and reply_message_id is None:
			raise HTTPException(status_code=422, detail="m2d send reply requires a mapped Discord message or explicit reply_to_message_id")
		if reply_message_id:
			body["message_reference"] = {"message_id": reply_message_id}
		return {
			"method": "POST",
			"url": f"{base}/channels/{channel_id}/messages",
			"body": body,
		}
	if not channel_id or not message_id:
		raise HTTPException(status_code=422, detail=f"m2d {action} requires a Discord channel/message mapping or explicit discord_channel_id and discord_message_id")
	if action == "edit":
		return {"method": "PATCH", "url": f"{base}/channels/{channel_id}/messages/{message_id}", "body": {"content": message_content(payload), "allowed_mentions": {"parse": []}}}
	if action == "delete":
		return {"method": "DELETE", "url": f"{base}/channels/{channel_id}/messages/{message_id}", "body": None}
	emoji = urllib.parse.quote(str(payload.get("emoji") or "?"), safe="")
	method = "DELETE" if action == "remove_reaction" else "PUT"
	return {"method": method, "url": f"{base}/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", "body": None}


def matrix_plan(action: str, payload: dict) -> dict:
	home = matrix_homeserver_url(payload) or "https://example.org"
	room_id, target_event_id = mapped_matrix_target(payload)
	if not room_id or MATRIX_ROOM_ID_RE.match(str(room_id)) is None:
		raise HTTPException(status_code=422, detail="d2m action requires a Matrix room_id")
	if action == "delete" or action == "remove_reaction":
		target = target_event_id or payload.get("event_id")
		if not target or MATRIX_EVENT_ID_RE.match(str(target)) is None:
			raise HTTPException(status_code=422, detail=f"d2m {action} requires target_event_id")
		txn_id = urllib.parse.quote(str(payload.get("txn_id") or stable_action_id("d2m", action, payload)[:24]), safe="")
		return {"method": "PUT", "url": f"{home}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/redact/{urllib.parse.quote(target, safe='')}/{txn_id}", "body": {"reason": payload.get("reason") or "Bridged from Discord"}}
	event_type = "m.reaction" if action == "reaction" else "m.room.message"
	txn_id = urllib.parse.quote(str(payload.get("txn_id") or stable_action_id("d2m", action, payload)[:24]), safe="")
	return {"method": "PUT", "url": f"{home}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/send/{urllib.parse.quote(event_type, safe='')}/{txn_id}", "body": matrix_event_content(payload, action)}


def execute_plan(direction: str, payload: dict, plan: dict):
	if direction == "m2d":
		token = discord_token(payload)
		if not token:
			return None, "missing Discord token; action queued"
		headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": "discord-surface-webui-matrix-bridge"}
	else:
		token = matrix_access_token(payload)
		home = matrix_homeserver_url(payload)
		if not token or not home:
			return None, "missing Matrix homeserver URL or access token; action queued"
		separator = "&" if "?" in plan["url"] else "?"
		plan = {**plan, "url": plan["url"] + separator + urllib.parse.urlencode({"access_token": token})}
		headers = {"Content-Type": "application/json", "User-Agent": "discord-surface-webui-matrix-bridge"}
	response = request_json(plan["method"], plan["url"], headers, plan.get("body"))
	return response, None


def run_action(direction: str, action: str, payload: dict):
	validate_direction_action(direction, action)
	plan = discord_plan(action, payload) if direction == "m2d" else matrix_plan(action, payload)
	if payload.get("dry_run") is True:
		return insert_or_update_action(direction, action, payload, "planned", plan)
	response, missing = execute_plan(direction, payload, plan)
	if missing:
		return insert_or_update_action(direction, action, payload, "pending", plan, error=missing)
	status = "sent" if response and 200 <= response["status"] < 300 else "failed"
	record_mapping(direction, action, payload, response)
	return insert_or_update_action(direction, action, payload, status, plan, response=response)


async def read_payload(request: Request) -> dict:
	payload = await request.json()
	if not isinstance(payload, dict):
		raise HTTPException(status_code=400, detail="body must be a JSON object")
	return payload


@router.post("/bridge/actions/{direction}/{action}")
async def post_bridge_action(direction: str, action: str, request: Request):
	payload = await read_payload(request)
	return run_action(direction, action, payload)


@router.post("/bridge/actions/batch")
async def post_bridge_action_batch(request: Request):
	payload = await read_payload(request)
	items = payload.get("actions")
	if not isinstance(items, list):
		raise HTTPException(status_code=400, detail="actions must be a list")
	results = []
	for item in items:
		if not isinstance(item, dict):
			raise HTTPException(status_code=400, detail="each action must be an object")
		direction = item.get("direction")
		action = item.get("action")
		if not isinstance(direction, str) or not isinstance(action, str):
			raise HTTPException(status_code=422, detail="each action requires direction and action")
		action_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {key: value for key, value in item.items() if key not in ("direction", "action")}
		results.append(run_action(direction, action, action_payload))
	return {"processed": len(results), "results": results}


@router.post("/bridge/actions/refresh")
async def post_bridge_action_refresh(request: Request):
	payload = await read_payload(request)
	limit = int(payload.get("limit") or 25)
	limit = max(1, min(limit, 100))
	pending = list(action_collection().find({"status": "pending"}).sort("created_at", 1).limit(limit))
	results = []
	for item in pending:
		results.append(run_action(item["direction"], item["action"], item.get("payload") or {}))
	return {"processed": len(results), "pending_before": len(pending), "results": results}
