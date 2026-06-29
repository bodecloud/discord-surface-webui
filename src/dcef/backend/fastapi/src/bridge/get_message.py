import re
import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

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


MATRIX_SERVER_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")


def bridge_event_payload(message: dict, raw: dict, metadata: dict | None = None, relation: dict | None = None):
	bridge = message.get("bridge") or {}
	matrix = bridge if bridge.get("platform") == "matrix" else {}
	content = raw.get("content") or {}
	event_metadata = metadata or raw.get("ooye_metadata") or {
		"sender": matrix.get("sender") or raw.get("sender"),
		"event_id": matrix.get("event_id") or raw.get("event_id"),
		"event_type": matrix.get("event_type") or raw.get("type"),
		"event_subtype": matrix.get("event_subtype") or content.get("msgtype"),
		"part": 0,
		"reaction_part": 0,
		"room_id": matrix.get("room_id") or raw.get("room_id"),
		"source": 0,
	}
	payload = {
		"metadata": event_metadata,
		"raw": raw,
	}
	if relation is not None:
		payload["relation"] = relation
	return payload


def bridge_events_from_message(message: dict):
	bridge = message.get("bridge") or {}
	matrix = bridge if bridge.get("platform") == "matrix" else {}
	raw = matrix.get("raw") or {}
	events = [bridge_event_payload(message, raw)]
	for related in matrix.get("related_events") or []:
		related_raw = related.get("raw") or {}
		events.append(bridge_event_payload(message, related_raw, relation={
			"event_id": related.get("event_id"),
			"event_type": related.get("event_type"),
			"relation_type": related.get("relation_type"),
			"sender": related.get("sender"),
		}))
	return events


def response_for_message(message: dict):
	source = "matrix" if (message.get("bridge") or {}).get("platform") == "matrix" else "discord"
	response = {
		"source": source,
		"events": bridge_events_from_message(message) if source == "matrix" else [],
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


@router.get("/bridge/mxc/{server}/{media_id:path}")
async def get_matrix_media(server: str, media_id: str):
	"""
	Redirect Matrix content URIs to the homeserver media download endpoint.

	The importer stores `mxc://server/media` as `/api/bridge/mxc/server/media`
	so the existing Discord-shaped attachment renderer can display Matrix media.
	"""
	server = urllib.parse.unquote(server)
	media_id = urllib.parse.unquote(media_id)
	if MATRIX_SERVER_RE.match(server) is None or media_id.strip("/") == "":
		raise HTTPException(status_code=422, detail="invalid Matrix media URI")
	encoded_server = urllib.parse.quote(server, safe="")
	encoded_media = urllib.parse.quote(media_id.strip("/"), safe="")
	return RedirectResponse(
		f"https://{server}/_matrix/media/v3/download/{encoded_server}/{encoded_media}",
		status_code=307,
	)
