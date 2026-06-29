import re

from fastapi import APIRouter, HTTPException

from ..common.Database import Database
from ..common.helpers import pad_id

router = APIRouter(
	prefix="",
	tags=["bridge"]
)


def find_message(message_id: str):
	guilds = list(Database.get_global_collection("guilds").find({}, {"_id": 1}))
	for guild in guilds:
		collection = Database.get_guild_collection(guild["_id"], "messages")
		message = collection.find_one({"_id": pad_id(message_id)})
		if message is not None:
			return message
	return None


def matrix_author_from_message(message: dict):
	author = message.get("author") or {}
	bridge = author.get("bridge") or {}
	if bridge.get("platform") != "matrix":
		return None
	avatar = author.get("avatar") or {}
	return {
		"displayname": author.get("nickname") or author.get("name"),
		"avatar_url": avatar.get("path") or avatar.get("remotePath"),
		"mxid": bridge.get("mxid") or author.get("name"),
	}


def bridge_event_from_message(message: dict):
	bridge = message.get("bridge") or {}
	matrix = bridge if bridge.get("platform") == "matrix" else {}
	raw = matrix.get("raw") or {}
	content = raw.get("content") or {}
	metadata = raw.get("ooye_metadata") or {
		"sender": matrix.get("sender") or raw.get("sender"),
		"event_id": matrix.get("event_id") or raw.get("event_id"),
		"event_type": matrix.get("event_type") or raw.get("type"),
		"event_subtype": matrix.get("event_subtype") or content.get("msgtype"),
		"part": 0,
		"reaction_part": 0,
		"room_id": matrix.get("room_id") or raw.get("room_id"),
		"source": 0,
	}
	return {
		"metadata": metadata,
		"raw": raw,
	}


def response_for_message(message: dict):
	source = "matrix" if (message.get("bridge") or {}).get("platform") == "matrix" else "discord"
	response = {
		"source": source,
		"events": [bridge_event_from_message(message)] if source == "matrix" else [],
	}
	author = matrix_author_from_message(message)
	if author is not None:
		response["matrix_author"] = author
	return response


@router.get("/message")
@router.get("/bridge/message")
async def get_bridge_message(message_id: str):
	"""
	OOYE-compatible message lookup.

	Out Of Your Element exposes GET /api/message?message_id=<discord id>.
	DCEF keeps the same path and response shape for normalized Matrix events.
	"""
	if re.match(r"^\d+$", message_id) is None:
		raise HTTPException(status_code=422, detail="message_id must be numeric")
	message = find_message(message_id)
	if message is None:
		raise HTTPException(status_code=404, detail="message not found")
	return response_for_message(message)
