import functools
import hashlib
import json
import os
from datetime import datetime, timezone

from pymongo import UpdateOne

from Formatters import Formatters

print = functools.partial(print, flush=True)


MATRIX_GUILD_ID = Formatters.pad_id(2)
MATRIX_GUILD_NAME = "Matrix Bridge"


class MatrixProcessor:
	"""
	Imports Matrix Client-Server exports and OOYE API payloads into DCEF's
	Discord-shaped Mongo collections.

	The frontend only knows guilds, channels, authors, and messages. This adapter
	keeps that contract while preserving Matrix-native IDs in `bridge.matrix`.
	"""
	def __init__(self, database, file_finder, json_path: str, asset_processor, index: int, total: int):
		self.database = database
		self.file_finder = file_finder
		self.json_path = json_path
		self.asset_processor = asset_processor
		self.index = index
		self.total = total
		self.collections = self.database.get_guild_collections(MATRIX_GUILD_ID)
		self.asset_processor.set_guild_id(MATRIX_GUILD_ID)
		self.database.create_indexes(MATRIX_GUILD_ID)

	@staticmethod
	def numeric_id(value: str, namespace: str = "matrix") -> str:
		digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()
		return Formatters.pad_id(int(digest, 16) % (10 ** 24))

	@staticmethod
	def event_sortable_id(event: dict, sequence: int) -> str:
		origin_ts = event.get("origin_server_ts")
		if isinstance(origin_ts, (int, float)) and origin_ts > 0:
			# Millisecond timestamps sort chronologically; leave room for same-ms events.
			return Formatters.pad_id(int(origin_ts) * 10000 + (sequence % 10000))
		event_id = event.get("event_id") or json.dumps(event, sort_keys=True)
		return MatrixProcessor.numeric_id(event_id, "matrix-event")

	@staticmethod
	def iso_from_ts(origin_server_ts) -> str:
		if isinstance(origin_server_ts, (int, float)) and origin_server_ts > 0:
			return datetime.fromtimestamp(origin_server_ts / 1000, tz=timezone.utc).isoformat()
		return datetime.now(timezone.utc).isoformat()

	@staticmethod
	def default_avatar_asset(seed: str) -> dict:
		avatar = f"https://api.dicebear.com/9.x/initials/svg?seed={seed}"
		return MatrixProcessor.remote_asset(avatar, "svg", "image")

	@staticmethod
	def remote_asset(url: str, extension: str = None, filetype: str = "unknown", size=None, width=None, height=None) -> dict:
		if extension is None:
			extension = url.split("?", 1)[0].rsplit(".", 1)[-1].lower() if "." in url.split("?", 1)[0] else None
		filename = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12].upper()
		if extension:
			filename = f"{filename}.{extension}"
		return {
			"_id": filename,
			"originalPath": url,
			"localPath": None,
			"remotePath": url,
			"path": url,
			"extension": extension,
			"type": filetype,
			"width": width,
			"height": height,
			"sizeBytes": size,
			"filenameWithHash": filename,
			"filenameWithoutHash": filename,
			"colorDominant": None,
			"colorPalette": None,
			"searchable": False
		}

	@staticmethod
	def load_matrix_payload(path: str):
		with open(path, "r", encoding="utf-8") as handle:
			return json.load(handle)

	@staticmethod
	def looks_like_matrix_payload(payload) -> bool:
		if not isinstance(payload, dict):
			return False
		if "events" in payload and isinstance(payload["events"], list):
			return any(MatrixProcessor.looks_like_matrix_event(item) for item in payload["events"])
		if "chunk" in payload and isinstance(payload["chunk"], list):
			return any(MatrixProcessor.looks_like_matrix_event(item) for item in payload["chunk"])
		if "rooms" in payload and isinstance(payload["rooms"], dict):
			return True
		return MatrixProcessor.looks_like_matrix_event(payload)

	@staticmethod
	def looks_like_matrix_event(event) -> bool:
		return isinstance(event, dict) and (
			("event_id" in event and "room_id" in event and "sender" in event)
			or ("raw" in event and isinstance(event["raw"], dict) and "event_id" in event["raw"])
		)

	@staticmethod
	def extract_events(payload) -> list:
		if MatrixProcessor.looks_like_matrix_event(payload):
			return [payload.get("raw", payload)]
		if isinstance(payload.get("events"), list):
			events = []
			for item in payload["events"]:
				if MatrixProcessor.looks_like_matrix_event(item):
					raw = item.get("raw", item)
					if payload.get("matrix_author") and "matrix_author" not in raw:
						raw["matrix_author"] = payload["matrix_author"]
					if item.get("metadata") and "ooye_metadata" not in raw:
						raw["ooye_metadata"] = item["metadata"]
					events.append(raw)
			return events
		if isinstance(payload.get("chunk"), list):
			return [item.get("raw", item) for item in payload["chunk"] if MatrixProcessor.looks_like_matrix_event(item)]
		events = []
		rooms = payload.get("rooms")
		if isinstance(rooms, dict):
			for room_id, room in rooms.items():
				for key in ("timeline", "events", "chunk"):
					items = room.get(key, []) if isinstance(room, dict) else []
					if isinstance(items, dict):
						items = items.get("events", items.get("chunk", []))
					for item in items:
						if MatrixProcessor.looks_like_matrix_event(item):
							raw = item.get("raw", item)
							raw.setdefault("room_id", room_id)
							events.append(raw)
		return events

	@staticmethod
	def is_message_event(event: dict) -> bool:
		content = event.get("content") or {}
		return event.get("type") in ("m.room.message", "m.sticker") and isinstance(content, dict)

	@staticmethod
	def body_from_event(event: dict) -> str:
		content = event.get("content") or {}
		body = content.get("body") or content.get("formatted_body") or ""
		if content.get("msgtype") in ("m.image", "m.video", "m.audio", "m.file") and content.get("url"):
			body = body or content.get("filename") or content.get("url")
			if content["url"].startswith("mxc://"):
				body = f"{body}\n{content['url']}"
		return body

	@staticmethod
	def attachment_from_event(event: dict):
		content = event.get("content") or {}
		msgtype = content.get("msgtype")
		url = content.get("external_url") or content.get("url")
		if msgtype not in ("m.image", "m.video", "m.audio", "m.file") or not url:
			return None
		info = content.get("info") or {}
		if url.startswith("http://") or url.startswith("https://"):
			filetype = {
				"m.image": "image",
				"m.video": "video",
				"m.audio": "audio",
				"m.file": "unknown",
			}.get(msgtype, "unknown")
			return MatrixProcessor.remote_asset(
				url,
				filetype=filetype,
				size=info.get("size"),
				width=info.get("w"),
				height=info.get("h"),
			)
		return None

	def author_from_event(self, event: dict, guild_id: str) -> dict:
		mxid = event.get("sender") or event.get("user_id") or "@unknown:matrix"
		displayname = (
			event.get("unsigned", {}).get("sender_display_name")
			or event.get("matrix_author", {}).get("displayname")
			or mxid.split(":", 1)[0].lstrip("@")
		)
		avatar_url = event.get("matrix_author", {}).get("avatar_url") or event.get("unsigned", {}).get("sender_avatar_url")
		avatar = MatrixProcessor.remote_asset(avatar_url, filetype="image") if avatar_url else MatrixProcessor.default_avatar_asset(displayname)
		return {
			"_id": MatrixProcessor.numeric_id(mxid, "matrix-user"),
			"name": mxid,
			"nickname": displayname,
			"color": None,
			"isBot": False,
			"avatar": avatar,
			"guildIds": [guild_id],
			"names": [mxid],
			"nicknames": [displayname],
			"bridge": {
				"platform": "matrix",
				"mxid": mxid,
			}
		}

	def event_to_message(self, event: dict, room_id: str, channel_id: str, channel_name: str, exported_at: str, sequence: int) -> dict:
		author = self.author_from_event(event, MATRIX_GUILD_ID)
		timestamp = MatrixProcessor.iso_from_ts(event.get("origin_server_ts"))
		content = event.get("content") or {}
		message = {
			"_id": MatrixProcessor.event_sortable_id(event, sequence),
			"type": "Default",
			"timestamp": timestamp,
			"timestampEdited": None,
			"isPinned": False,
			"content": [{"timestamp": timestamp, "content": MatrixProcessor.body_from_event(event)}],
			"author": author,
			"guildId": MATRIX_GUILD_ID,
			"channelId": channel_id,
			"channelName": channel_name,
			"exportedAt": exported_at,
			"sources": [hashlib.sha256(self.json_path.encode("utf-8")).hexdigest()[:10].upper()],
			"bridge": {
				"platform": "matrix",
				"room_id": room_id,
				"event_id": event.get("event_id"),
				"event_type": event.get("type"),
				"event_subtype": content.get("msgtype"),
				"sender": event.get("sender"),
				"raw": event,
			}
		}
		attachment = MatrixProcessor.attachment_from_event(event)
		if attachment is not None:
			message["attachments"] = [attachment]
		return message

	def room_name(self, room_id: str, events: list, payload: dict) -> str:
		if isinstance(payload.get("room_name"), str):
			return payload["room_name"]
		for event in reversed(events):
			if event.get("room_id") != room_id:
				continue
			content = event.get("content") or {}
			if event.get("type") == "m.room.name" and content.get("name"):
				return content["name"]
			if event.get("unsigned", {}).get("room_name"):
				return event["unsigned"]["room_name"]
		return room_id

	def upsert_author(self, author: dict):
		existing = self.collections["authors"].find_one({"_id": author["_id"]})
		if existing is None:
			author["msg_count"] = 0
			self.collections["authors"].insert_one(author)
			return
		self.collections["authors"].update_one({"_id": author["_id"]}, {"$set": {
			"name": author["name"],
			"nickname": author["nickname"],
			"avatar": author["avatar"],
			"bridge": author["bridge"],
			"guildIds": sorted(set(existing.get("guildIds", []) + author["guildIds"])),
			"names": sorted(set(existing.get("names", []) + author["names"])),
			"nicknames": sorted(set(existing.get("nicknames", []) + author["nicknames"])),
		}})

	def mark_as_processed(self):
		json_path_with_base = self.file_finder.add_base_directory(self.json_path)
		self.collections["jsons"].replace_one({"_id": self.json_path}, {"_id": self.json_path, "size": os.path.getsize(json_path_with_base), "date_modified": os.path.getmtime(json_path_with_base), "source": "matrix"}, upsert=True)

	def process(self):
		path = self.file_finder.add_base_directory(self.json_path)
		payload = MatrixProcessor.load_matrix_payload(path)
		if not MatrixProcessor.looks_like_matrix_payload(payload):
			print("invalid matrix file " + self.json_path)
			return

		events = [event for event in MatrixProcessor.extract_events(payload) if MatrixProcessor.is_message_event(event)]
		if len(events) == 0:
			print("matrix file has no supported message events " + self.json_path)
			self.mark_as_processed()
			return

		exported_at = datetime.now(timezone.utc).isoformat()
		guild = {
			"_id": MATRIX_GUILD_ID,
			"name": MATRIX_GUILD_NAME,
			"icon": MatrixProcessor.default_avatar_asset("Matrix Bridge"),
			"exported_at": exported_at,
			"exportedAt": exported_at,
			"bridge": {"platform": "matrix", "source": "out-of-your-element-compatible"},
		}
		existing_guild = self.collections["guilds"].find_one({"_id": MATRIX_GUILD_ID}) or {}
		guild["msg_count"] = existing_guild.get("msg_count", 0)
		self.collections["guilds"].replace_one({"_id": MATRIX_GUILD_ID}, guild, upsert=True)

		events_by_room = {}
		for event in events:
			room_id = event.get("room_id") or payload.get("room_id") or "!unknown:matrix"
			events_by_room.setdefault(room_id, []).append(event)

		for room_id, room_events in events_by_room.items():
			channel_id = MatrixProcessor.numeric_id(room_id, "matrix-room")
			channel_name = self.room_name(room_id, events, payload)
			channel = {
				"_id": channel_id,
				"type": "GuildTextChat",
				"categoryId": None,
				"category": "Matrix",
				"name": channel_name,
				"topic": None,
				"guildId": MATRIX_GUILD_ID,
				"exportedAt": exported_at,
				"bridge": {
					"platform": "matrix",
					"room_id": room_id,
				}
			}
			existing_channel = self.collections["channels"].find_one({"_id": channel_id}) or {}
			channel["msg_count"] = existing_channel.get("msg_count", 0)
			self.collections["channels"].replace_one({"_id": channel_id}, channel, upsert=True)

			messages = [self.event_to_message(event, room_id, channel_id, channel_name, exported_at, index) for index, event in enumerate(sorted(room_events, key=lambda item: item.get("origin_server_ts", 0)))]
			for message in messages:
				self.upsert_author(message["author"])
			if messages:
				self.collections["messages"].bulk_write([UpdateOne({"_id": message["_id"]}, {"$set": message}, upsert=True) for message in messages])

			self.collections["channels"].update_one({"_id": channel_id}, {"$set": {"msg_count": self.collections["messages"].count_documents({"channelId": channel_id})}})

		self.collections["guilds"].update_one({"_id": MATRIX_GUILD_ID}, {"$set": {"msg_count": self.collections["messages"].count_documents({})}})
		for author in self.collections["authors"].find({"guildIds": MATRIX_GUILD_ID}, {"_id": 1}):
			self.collections["authors"].update_one({"_id": author["_id"]}, {"$set": {"msg_count": self.collections["messages"].count_documents({"author._id": author["_id"]})}})

		self.mark_as_processed()
