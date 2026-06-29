import functools
import hashlib
import json
import os
import re
import urllib.parse
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
		self.room_state = {}
		self.member_profiles = {}

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
	def mxc_to_proxy_url(url: str) -> str:
		if not isinstance(url, str) or not url.startswith("mxc://"):
			return url
		parts = url.removeprefix("mxc://").split("/", 1)
		if len(parts) != 2 or not parts[0] or not parts[1]:
			return url
		server = urllib.parse.quote(parts[0], safe="")
		media_id = urllib.parse.quote(parts[1], safe="")
		return f"/api/bridge/mxc/{server}/{media_id}"

	@staticmethod
	def emoji_asset(key: str) -> dict:
		codepoints = "-".join(f"{ord(char):x}" for char in key if ord(char) != 0xfe0f)
		if codepoints:
			url = f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/svg/{codepoints}.svg"
			return MatrixProcessor.remote_asset(url, "svg", "image")
		return MatrixProcessor.default_avatar_asset(key or "reaction")

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
			or ("type" in event and "state_key" in event and "content" in event)
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
					if payload.get("room_id"):
						raw.setdefault("room_id", payload["room_id"])
					if payload.get("matrix_author") and "matrix_author" not in raw:
						raw["matrix_author"] = payload["matrix_author"]
					if item.get("metadata") and "ooye_metadata" not in raw:
						raw["ooye_metadata"] = item["metadata"]
					events.append(raw)
			return events
		if isinstance(payload.get("chunk"), list):
			return [item.get("raw", item) for item in payload["chunk"] if MatrixProcessor.looks_like_matrix_event(item)]
		if isinstance(payload.get("state"), list):
			events = []
			for item in payload["state"]:
				if MatrixProcessor.looks_like_matrix_event(item):
					raw = item.get("raw", item)
					if payload.get("room_id"):
						raw.setdefault("room_id", payload["room_id"])
					events.append(raw)
			return events
		events = []
		rooms = payload.get("rooms")
		if isinstance(rooms, dict):
			for room_id, room in rooms.items():
				for key in ("state", "timeline", "events", "chunk", "invite_state"):
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
		return event.get("type") in ("m.room.message", "m.sticker") and isinstance(content, dict) and not MatrixProcessor.is_edit_event(event)

	@staticmethod
	def relation(event: dict) -> dict:
		content = event.get("content") or {}
		relation = content.get("m.relates_to") or content.get("m.relationship") or {}
		return relation if isinstance(relation, dict) else {}

	@staticmethod
	def reply_to_event_id(event: dict) -> str | None:
		in_reply_to = MatrixProcessor.relation(event).get("m.in_reply_to") or {}
		if isinstance(in_reply_to, dict):
			return in_reply_to.get("event_id")
		return None

	@staticmethod
	def is_edit_event(event: dict) -> bool:
		return MatrixProcessor.relation(event).get("rel_type") == "m.replace" and "m.new_content" in (event.get("content") or {})

	@staticmethod
	def is_reaction_event(event: dict) -> bool:
		return event.get("type") == "m.reaction" and MatrixProcessor.relation(event).get("rel_type") == "m.annotation"

	@staticmethod
	def is_redaction_event(event: dict) -> bool:
		return event.get("type") == "m.room.redaction" or event.get("redacts") or (event.get("content") or {}).get("redacts")

	@staticmethod
	def target_event_id(event: dict) -> str | None:
		return event.get("redacts") or (event.get("content") or {}).get("redacts") or MatrixProcessor.relation(event).get("event_id")

	@staticmethod
	def event_room_id(event: dict, fallback: str | None = None) -> str:
		return event.get("room_id") or fallback or "!unknown:matrix"

	@staticmethod
	def room_state_key(event: dict) -> tuple[str | None, str, str]:
		return (event.get("room_id"), event.get("type") or "", event.get("state_key") or "")

	@staticmethod
	def state_sort_value(event: dict) -> int | float:
		value = event.get("origin_server_ts")
		return value if isinstance(value, (int, float)) else 0

	@staticmethod
	def media_url_from_content(content: dict) -> str | None:
		file_info = content.get("file") if isinstance(content.get("file"), dict) else {}
		return content.get("external_url") or content.get("url") or file_info.get("url")

	@staticmethod
	def asset_from_mxc_or_url(url: str | None, extension: str = None, filetype: str = "image", size=None, width=None, height=None):
		if not url:
			return None
		if url.startswith("mxc://"):
			url = MatrixProcessor.mxc_to_proxy_url(url)
		if url.startswith("http://") or url.startswith("https://") or url.startswith("/api/bridge/mxc/"):
			return MatrixProcessor.remote_asset(url, extension=extension, filetype=filetype, size=size, width=width, height=height)
		return None

	@staticmethod
	def body_from_event(event: dict) -> str:
		content = event.get("content") or {}
		if MatrixProcessor.is_edit_event(event):
			content = content.get("m.new_content") or content
		body = content.get("body") or content.get("formatted_body") or ""
		if MatrixProcessor.reply_to_event_id(event):
			formatted = content.get("formatted_body")
			if isinstance(formatted, str) and formatted.startswith("<mx-reply>"):
				formatted = re.sub(r"^<mx-reply>.*?</mx-reply>", "", formatted, flags=re.DOTALL)
				if not content.get("body"):
					body = re.sub(r"<[^>]+>", "", formatted or "")
			if isinstance(body, str):
				lines = body.split("\n")
				stage = 0
				for index, line in enumerate(lines):
					if stage >= 0 and line.startswith(">"):
						stage = 1
						continue
					if stage >= 1 and line.strip() == "":
						stage = 2
						continue
					if stage == 2 and line.strip() != "":
						body = "\n".join(lines[index:])
						break
		media_url = MatrixProcessor.media_url_from_content(content)
		if content.get("msgtype") in ("m.image", "m.video", "m.audio", "m.file") and media_url:
			body = body or content.get("filename") or media_url
			if media_url.startswith("mxc://"):
				body = f"{body}\n{MatrixProcessor.mxc_to_proxy_url(media_url)}"
		return body

	@staticmethod
	def attachment_from_event(event: dict):
		content = event.get("content") or {}
		msgtype = content.get("msgtype")
		url = MatrixProcessor.media_url_from_content(content)
		if msgtype not in ("m.image", "m.video", "m.audio", "m.file") or not url:
			return None
		info = content.get("info") or {}
		filetype = {
			"m.image": "image",
			"m.video": "video",
			"m.audio": "audio",
			"m.file": "unknown",
		}.get(msgtype, "unknown")
		return MatrixProcessor.asset_from_mxc_or_url(
			url,
			filetype=filetype,
			size=info.get("size"),
			width=info.get("w"),
			height=info.get("h"),
		)

	def reaction_from_event(self, event: dict) -> dict:
		key = MatrixProcessor.relation(event).get("key") or "?"
		author = self.author_from_event(event, MATRIX_GUILD_ID)
		return {
			"emoji": {
				"_id": MatrixProcessor.numeric_id(key, "matrix-reaction"),
				"name": key,
				"isAnimated": False,
				"image": MatrixProcessor.emoji_asset(key),
				"source": "default",
				"guildId": None,
			},
			"count": 1,
			"users": [{
				"_id": author["_id"],
				"name": author["name"],
				"nickname": author["nickname"],
				"isBot": author["isBot"],
				"avatar": author["avatar"],
			}],
			"bridge": {
				"platform": "matrix",
				"event_id": event.get("event_id"),
				"room_id": event.get("room_id"),
				"sender": event.get("sender"),
			}
		}

	def author_from_event(self, event: dict, guild_id: str) -> dict:
		mxid = event.get("sender") or event.get("user_id") or "@unknown:matrix"
		room_id = event.get("room_id")
		member_profile = self.member_profiles.get((room_id, mxid), {}) or self.member_profiles.get((None, mxid), {})
		displayname = (
			event.get("unsigned", {}).get("sender_display_name")
			or event.get("matrix_author", {}).get("displayname")
			or member_profile.get("displayname")
			or mxid.split(":", 1)[0].lstrip("@")
		)
		avatar_url = (
			event.get("matrix_author", {}).get("avatar_url")
			or event.get("unsigned", {}).get("sender_avatar_url")
			or member_profile.get("avatar_url")
		)
		avatar = MatrixProcessor.asset_from_mxc_or_url(avatar_url, filetype="image") if avatar_url else MatrixProcessor.default_avatar_asset(displayname)
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
				"room_id": room_id,
				"membership": member_profile.get("membership"),
			}
		}

	def event_to_message(self, event: dict, room_id: str, channel_id: str, channel_name: str, exported_at: str, sequence: int) -> dict:
		author = self.author_from_event(event, MATRIX_GUILD_ID)
		timestamp = MatrixProcessor.iso_from_ts(event.get("origin_server_ts"))
		content = event.get("content") or {}
		reply_to_event_id = MatrixProcessor.reply_to_event_id(event)
		message = {
			"_id": MatrixProcessor.event_sortable_id(event, sequence),
			"type": "Reply" if reply_to_event_id else "Default",
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
				"reply_to_event_id": reply_to_event_id,
				"raw": event,
			}
		}
		attachment = MatrixProcessor.attachment_from_event(event)
		if attachment is not None:
			message["attachments"] = [attachment]
		return message

	def find_message_by_event_id(self, event_id: str):
		if event_id is None:
			return None
		return self.collections["messages"].find_one({"bridge.event_id": event_id})

	def append_related_event(self, message: dict, event: dict, relation_type: str):
		related = (message.get("bridge") or {}).get("related_events") or []
		if event.get("event_id") in [item.get("event_id") for item in related]:
			return related
		related.append({
			"event_id": event.get("event_id"),
			"event_type": event.get("type"),
			"relation_type": relation_type,
			"sender": event.get("sender"),
			"origin_server_ts": event.get("origin_server_ts"),
			"raw": event,
		})
		return related

	def apply_edit(self, event: dict):
		target = self.find_message_by_event_id(MatrixProcessor.target_event_id(event))
		if target is None:
			return
		new_content = (event.get("content") or {}).get("m.new_content") or {}
		edited_at = MatrixProcessor.iso_from_ts(event.get("origin_server_ts"))
		content_history = target.get("content") or []
		prior_content = content_history[0] if content_history else None
		updated_content = {
			"timestamp": edited_at,
			"content": MatrixProcessor.body_from_event({**event, "content": new_content}),
		}
		new_history = [updated_content]
		if prior_content is not None and prior_content.get("content") != updated_content["content"]:
			new_history.append(prior_content)
		new_history.extend(content_history[1:])
		self.collections["messages"].update_one({"_id": target["_id"]}, {"$set": {
			"content": new_history,
			"timestampEdited": edited_at,
			"bridge.related_events": self.append_related_event(target, event, "m.replace"),
		}})

	def apply_reaction(self, event: dict):
		target = self.find_message_by_event_id(MatrixProcessor.target_event_id(event))
		if target is None:
			return
		reaction = self.reaction_from_event(event)
		reactions = target.get("reactions") or []
		existing = next((item for item in reactions if item.get("emoji", {}).get("_id") == reaction["emoji"]["_id"]), None)
		if existing is None:
			reactions.append(reaction)
		else:
			users = existing.get("users") or []
			if reaction["users"][0]["_id"] not in [user.get("_id") for user in users]:
				users.append(reaction["users"][0])
			existing["users"] = users
			existing["count"] = len(users)
		self.collections["messages"].update_one({"_id": target["_id"]}, {"$set": {
			"reactions": reactions,
			"bridge.related_events": self.append_related_event(target, event, "m.annotation"),
		}})

	def apply_redaction(self, event: dict):
		target = self.find_message_by_event_id(MatrixProcessor.target_event_id(event))
		if target is None:
			return
		self.collections["messages"].update_one({"_id": target["_id"]}, {"$set": {
			"isDeleted": True,
			"bridge.related_events": self.append_related_event(target, event, "m.room.redaction"),
		}})

	def apply_reply_reference(self, event: dict):
		reply_to_event_id = MatrixProcessor.reply_to_event_id(event)
		if reply_to_event_id is None:
			return
		message = self.find_message_by_event_id(event.get("event_id"))
		if message is None:
			return
		target = self.find_message_by_event_id(reply_to_event_id)
		if target is not None:
			reference = {
				"type": "Reply",
				"messageId": target["_id"],
				"channelId": target["channelId"],
				"guildId": target["guildId"],
			}
		else:
			reference = {
				"type": "Reply",
				"messageId": MatrixProcessor.numeric_id(reply_to_event_id, "matrix-reply-missing"),
				"channelId": message["channelId"],
				"guildId": message["guildId"],
			}
		self.collections["messages"].update_one({"_id": message["_id"]}, {"$set": {
			"type": "Reply",
			"reference": reference,
			"bridge.reply_to_event_id": reply_to_event_id,
		}})

	def apply_related_events(self, events: list):
		for event in sorted(events, key=lambda item: item.get("origin_server_ts", 0)):
			self.apply_reply_reference(event)
			if MatrixProcessor.is_edit_event(event):
				self.apply_edit(event)
			elif MatrixProcessor.is_reaction_event(event):
				self.apply_reaction(event)
			elif MatrixProcessor.is_redaction_event(event):
				self.apply_redaction(event)

	def build_room_state(self, events: list, payload: dict) -> dict:
		latest = {}
		for event in sorted(events, key=MatrixProcessor.state_sort_value):
			if "state_key" not in event:
				continue
			latest[MatrixProcessor.room_state_key(event)] = event

		state_by_room = {}
		for (room_id, event_type, state_key), event in latest.items():
			if not room_id:
				continue
			room = state_by_room.setdefault(room_id, {
				"name": None,
				"topic": None,
				"avatar": None,
				"is_space": False,
				"parents": [],
				"children": [],
				"members": {},
			})
			content = event.get("content") or {}
			if event_type == "m.room.name" and content.get("name"):
				room["name"] = content["name"]
			elif event_type == "m.room.topic" and content.get("topic"):
				room["topic"] = content["topic"]
			elif event_type == "m.room.avatar":
				room["avatar"] = MatrixProcessor.asset_from_mxc_or_url(content.get("url"), filetype="image")
			elif event_type == "m.room.create" and content.get("type") == "m.space":
				room["is_space"] = True
			elif event_type == "m.space.child" and state_key:
				room["children"].append({
					"room_id": state_key,
					"via": content.get("via") if isinstance(content.get("via"), list) else [],
					"suggested": bool(content.get("suggested")),
					"order": content.get("order"),
				})
			elif event_type == "m.space.parent" and state_key:
				room["parents"].append({
					"room_id": state_key,
					"via": content.get("via") if isinstance(content.get("via"), list) else [],
					"canonical": bool(content.get("canonical")),
				})
			elif event_type == "m.room.member" and state_key:
				room["members"][state_key] = {
					"displayname": content.get("displayname"),
					"avatar_url": content.get("avatar_url"),
					"membership": content.get("membership"),
				}

		if isinstance(payload.get("room_id"), str):
			room = state_by_room.setdefault(payload["room_id"], {
				"name": None,
				"topic": None,
				"avatar": None,
				"is_space": False,
				"parents": [],
				"children": [],
				"members": {},
			})
			if isinstance(payload.get("room_name"), str):
				room["name"] = payload["room_name"]
			if isinstance(payload.get("room_topic"), str):
				room["topic"] = payload["room_topic"]
			if isinstance(payload.get("room_avatar_url"), str):
				room["avatar"] = MatrixProcessor.asset_from_mxc_or_url(payload["room_avatar_url"], filetype="image")
		return state_by_room

	def populate_member_profiles(self, state_by_room: dict):
		self.member_profiles = {}
		for room_id, state in state_by_room.items():
			for mxid, profile in state.get("members", {}).items():
				self.member_profiles[(room_id, mxid)] = profile

	def room_parent_space(self, room_id: str, state_by_room: dict):
		room = state_by_room.get(room_id, {})
		parents = room.get("parents") or []
		if parents:
			canonical = next((parent for parent in parents if parent.get("canonical")), None)
			parent_room_id = (canonical or parents[0]).get("room_id")
			if parent_room_id:
				return parent_room_id, state_by_room.get(parent_room_id, {})
		for possible_parent_id, possible_parent in state_by_room.items():
			for child in possible_parent.get("children") or []:
				if child.get("room_id") == room_id:
					return possible_parent_id, possible_parent
		return None, None

	def room_name(self, room_id: str, events: list, payload: dict, state_by_room: dict = None) -> str:
		state = (state_by_room or {}).get(room_id, {})
		if state.get("name"):
			return state["name"]
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

	def room_topic(self, room_id: str, state_by_room: dict) -> str | None:
		return (state_by_room.get(room_id) or {}).get("topic")

	def room_avatar(self, room_id: str, state_by_room: dict):
		return (state_by_room.get(room_id) or {}).get("avatar")

	def room_category(self, room_id: str, state_by_room: dict) -> tuple[str | None, str]:
		parent_room_id, parent_state = self.room_parent_space(room_id, state_by_room)
		if parent_room_id:
			return MatrixProcessor.numeric_id(parent_room_id, "matrix-space"), parent_state.get("name") or parent_room_id
		return None, "Matrix"

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

	def process_payload(self, payload, mark_processed: bool = True):
		if not MatrixProcessor.looks_like_matrix_payload(payload):
			print("invalid matrix file " + self.json_path)
			return

		all_events = MatrixProcessor.extract_events(payload)
		events = [event for event in all_events if MatrixProcessor.is_message_event(event)]
		if len(events) == 0 and len(all_events) == 0:
			print("matrix file has no supported message events " + self.json_path)
			self.mark_as_processed()
			return

		state_by_room = self.build_room_state(all_events, payload)
		self.room_state = state_by_room
		self.populate_member_profiles(state_by_room)

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
			channel_name = self.room_name(room_id, events, payload, state_by_room)
			category_id, category_name = self.room_category(room_id, state_by_room)
			channel = {
				"_id": channel_id,
				"type": "GuildTextChat",
				"categoryId": category_id,
				"category": category_name,
				"name": channel_name,
				"topic": self.room_topic(room_id, state_by_room),
				"guildId": MATRIX_GUILD_ID,
				"exportedAt": exported_at,
				"bridge": {
					"platform": "matrix",
					"room_id": room_id,
					"avatar": self.room_avatar(room_id, state_by_room),
					"parents": (state_by_room.get(room_id) or {}).get("parents") or [],
					"children": (state_by_room.get(room_id) or {}).get("children") or [],
					"is_space": bool((state_by_room.get(room_id) or {}).get("is_space")),
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

		self.apply_related_events(all_events)

		self.collections["guilds"].update_one({"_id": MATRIX_GUILD_ID}, {"$set": {"msg_count": self.collections["messages"].count_documents({})}})
		for author in self.collections["authors"].find({"guildIds": MATRIX_GUILD_ID}, {"_id": 1}):
			self.collections["authors"].update_one({"_id": author["_id"]}, {"$set": {"msg_count": self.collections["messages"].count_documents({"author._id": author["_id"]})}})

		if mark_processed:
			self.mark_as_processed()

	def process(self):
		path = self.file_finder.add_base_directory(self.json_path)
		payload = MatrixProcessor.load_matrix_payload(path)
		self.process_payload(payload, mark_processed=True)
