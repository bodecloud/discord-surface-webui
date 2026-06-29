import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..common.Database import db

router = APIRouter(prefix="/archive", tags=["archive"])

INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9-]{2,128}$")
DISCORD_INVITE_URL = "https://discord.com/api/v10/invites/{code}"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
DISCORD_API_URL = "https://discord.com/api/v10"
DCE_CLI_PATH = os.getenv("DISCORD_CHAT_EXPORTER_CLI", "/dcef/tools/DiscordChatExporter.Cli")
DCE_LIVE_EXPORT_DIR = os.getenv("DISCORD_LIVE_EXPORT_DIR", "/dcef/live-exports")
RCLONE_RC_URL = os.getenv("RCLONE_RC_URL", "http://rclone:5572").rstrip("/")
ARCHIVE_ROOT_IN_RCLONE = "/hostfs/opt/docker/data/discord-ro"
BACKUP_SOURCES = {
	"exports": f"{ARCHIVE_ROOT_IN_RCLONE}/exports",
	"cache": f"{ARCHIVE_ROOT_IN_RCLONE}/cache",
}
MAX_LIVE_CHANNELS = int(os.getenv("DISCORD_LIVE_MAX_CHANNELS", "8"))
MAX_LIVE_MESSAGES_PER_CHANNEL = int(os.getenv("DISCORD_LIVE_MESSAGES_PER_CHANNEL", "50"))
DCE_INITIAL_LOOKBACK_DAYS = int(os.getenv("DISCORD_EXPORTER_INITIAL_LOOKBACK_DAYS", "14"))
DCE_CHANNEL_TIMEOUT_SECONDS = int(os.getenv("DISCORD_EXPORTER_CHANNEL_TIMEOUT_SECONDS", "90"))


class InviteSourceRequest(BaseModel):
	invite: str = Field(..., min_length=2, max_length=512)
	label: str | None = Field(default=None, max_length=128)


class BackupTargetRequest(BaseModel):
	name: str = Field(..., min_length=1, max_length=128)
	provider: str = Field(..., min_length=1, max_length=64)
	enabled: bool = True
	config: dict = Field(default_factory=dict)


class BackupRunRequest(BaseModel):
	source: str = "all"
	dry_run: bool = False


class RcloneRemoteRequest(BaseModel):
	name: str = Field(..., min_length=1, max_length=64)
	type: str = Field(..., min_length=1, max_length=32)
	parameters: dict = Field(default_factory=dict)


class DiscordConfigRequest(BaseModel):
	client_id: str | None = Field(default=None, max_length=256)
	client_secret: str | None = Field(default=None, max_length=512)
	redirect_uri: str | None = Field(default=None, max_length=512)
	bot_token: str | None = Field(default=None, max_length=512)
	importer_token: str | None = Field(default=None, max_length=1024)


def now_iso():
	return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_discord_timestamp(value: str | None):
	if not value:
		return None
	try:
		return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None


def extract_invite_code(invite: str) -> str:
	invite = invite.strip()
	parsed = urlparse(invite if "://" in invite else f"https://discord.gg/{invite}")
	if parsed.netloc and parsed.netloc.lower() not in {"discord.gg", "discord.com", "www.discord.com"}:
		raise HTTPException(status_code=400, detail="Only Discord invite links or invite codes are supported")
	parts = [part for part in parsed.path.split("/") if part]
	if not parts:
		raise HTTPException(status_code=400, detail="Invite code is missing")
	if parts[0] == "invite" and len(parts) > 1:
		code = parts[1]
	else:
		code = parts[0]
	if not INVITE_CODE_RE.match(code):
		raise HTTPException(status_code=400, detail="Invite code contains unsupported characters")
	return code


def fetch_invite_metadata(code: str) -> dict:
	response = requests.get(
		DISCORD_INVITE_URL.format(code=code),
		params={"with_counts": "true"},
		headers={"User-Agent": "discord-ro-archive/1.0"},
		timeout=15,
	)
	if response.status_code == 404:
		raise HTTPException(status_code=404, detail="Discord invite not found or expired")
	if response.status_code >= 400:
		raise HTTPException(status_code=502, detail=f"Discord invite lookup failed with HTTP {response.status_code}")
	payload = response.json()
	return {
		"code": payload.get("code", code),
		"type": payload.get("type"),
		"guild": payload.get("guild"),
		"channel": payload.get("channel"),
		"approximate_member_count": payload.get("approximate_member_count"),
		"approximate_presence_count": payload.get("approximate_presence_count"),
		"fetched_at": now_iso(),
	}


def stored_discord_config() -> dict:
	return db.archive_settings.find_one({"key": "discord_config"}, {"_id": 0}) or {}


def discord_config_value(name: str) -> str | None:
	env_name = {
		"client_id": "DISCORD_OAUTH_CLIENT_ID",
		"client_secret": "DISCORD_OAUTH_CLIENT_SECRET",
		"redirect_uri": "DISCORD_OAUTH_REDIRECT_URI",
		"bot_token": "DISCORD_BOT_TOKEN",
		"importer_token": "DISCORD_EXPORTER_TOKEN",
	}[name]
	return os.getenv(env_name) or stored_discord_config().get(name)


def public_discord_config() -> dict:
	config = stored_discord_config()
	return {
		"client_id_present": bool(os.getenv("DISCORD_OAUTH_CLIENT_ID") or config.get("client_id")),
		"client_secret_present": bool(os.getenv("DISCORD_OAUTH_CLIENT_SECRET") or config.get("client_secret")),
		"redirect_uri": discord_redirect_uri(),
		"bot_token_present": bool(os.getenv("DISCORD_BOT_TOKEN") or config.get("bot_token")),
		"importer_token_present": bool(os.getenv("DISCORD_EXPORTER_TOKEN") or config.get("importer_token")),
		"chat_exporter": {
			"available": Path(DCE_CLI_PATH).exists(),
			"path": DCE_CLI_PATH,
		},
		"env": {
			"client_id": bool(os.getenv("DISCORD_OAUTH_CLIENT_ID")),
			"client_secret": bool(os.getenv("DISCORD_OAUTH_CLIENT_SECRET")),
			"redirect_uri": bool(os.getenv("DISCORD_OAUTH_REDIRECT_URI")),
			"bot_token": bool(os.getenv("DISCORD_BOT_TOKEN")),
			"importer_token": bool(os.getenv("DISCORD_EXPORTER_TOKEN")),
		},
	}


def discord_oauth_configured() -> bool:
	return bool(discord_config_value("client_id") and discord_config_value("client_secret"))


def discord_redirect_uri() -> str:
	return discord_config_value("redirect_uri") or "https://discord-ro.bolabaden.org/api/archive/discord/oauth/callback"


def token_expiry(expires_in: int | float | None) -> str | None:
	if expires_in is None:
		return None
	return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(expires_in))).isoformat()


def exchange_discord_code(code: str) -> dict:
	response = requests.post(
		DISCORD_TOKEN_URL,
		data={
			"client_id": discord_config_value("client_id") or "",
			"client_secret": discord_config_value("client_secret") or "",
			"grant_type": "authorization_code",
			"code": code,
			"redirect_uri": discord_redirect_uri(),
		},
		headers={"Content-Type": "application/x-www-form-urlencoded"},
		timeout=20,
	)
	if response.status_code >= 400:
		raise HTTPException(status_code=502, detail=f"Discord OAuth token exchange failed with HTTP {response.status_code}")
	return response.json()


def refresh_discord_token(token_doc: dict) -> dict:
	response = requests.post(
		DISCORD_TOKEN_URL,
		data={
			"client_id": discord_config_value("client_id") or "",
			"client_secret": discord_config_value("client_secret") or "",
			"grant_type": "refresh_token",
			"refresh_token": token_doc.get("refresh_token", ""),
		},
		headers={"Content-Type": "application/x-www-form-urlencoded"},
		timeout=20,
	)
	if response.status_code >= 400:
		raise HTTPException(status_code=502, detail=f"Discord OAuth token refresh failed with HTTP {response.status_code}")
	return response.json()


def latest_oauth_token() -> dict | None:
	token_doc = db.archive_oauth_tokens.find_one({"provider": "discord"}, sort=[("created_at", -1)])
	if token_doc is None:
		return None
	expires_at = parse_discord_timestamp(token_doc.get("expires_at"))
	if expires_at and expires_at <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2):
		refreshed = refresh_discord_token(token_doc)
		token_doc.update(
			{
				"access_token": refreshed.get("access_token"),
				"refresh_token": refreshed.get("refresh_token", token_doc.get("refresh_token")),
				"token_type": refreshed.get("token_type", token_doc.get("token_type")),
				"scope": refreshed.get("scope", token_doc.get("scope")),
				"expires_at": token_expiry(refreshed.get("expires_in")),
				"refreshed_at": now_iso(),
			}
		)
		db.archive_oauth_tokens.update_one({"_id": token_doc["_id"]}, {"$set": token_doc})
	return token_doc


def discord_get(path: str, auth_header: str, params: dict | None = None) -> tuple[int, dict | list | str]:
	response = requests.get(
		f"{DISCORD_API_URL}{path}",
		params=params or {},
		headers={"Authorization": auth_header, "User-Agent": "discord-ro-archive/1.0"},
		timeout=20,
	)
	if response.status_code == 429:
		raise HTTPException(status_code=429, detail="Discord rate limit reached; existing archive cache was left untouched")
	if response.status_code >= 400:
		try:
			return response.status_code, response.json()
		except ValueError:
			return response.status_code, response.text
	return response.status_code, response.json()


def store_oauth_token(token_payload: dict) -> dict:
	access_token = token_payload.get("access_token")
	if not access_token:
		raise HTTPException(status_code=502, detail="Discord OAuth token response did not include an access token")
	status, user = discord_get("/users/@me", f"Bearer {access_token}")
	if status >= 400:
		raise HTTPException(status_code=502, detail=f"Discord user lookup failed with HTTP {status}")
	status, guilds = discord_get("/users/@me/guilds", f"Bearer {access_token}")
	if status >= 400:
		guilds = []
	document = {
		"provider": "discord",
		"created_at": now_iso(),
		"updated_at": now_iso(),
		"access_token": access_token,
		"refresh_token": token_payload.get("refresh_token"),
		"token_type": token_payload.get("token_type"),
		"scope": token_payload.get("scope"),
		"expires_at": token_expiry(token_payload.get("expires_in")),
		"user": {
			"id": user.get("id"),
			"username": user.get("username"),
			"global_name": user.get("global_name"),
		},
		"guild_ids": [guild.get("id") for guild in guilds if guild.get("id")],
		"guild_count": len(guilds) if isinstance(guilds, list) else 0,
	}
	db.archive_oauth_tokens.insert_one(document)
	return {key: value for key, value in document.items() if key not in {"_id", "access_token", "refresh_token"}}


def rclone_rc(path: str, payload: dict | None = None) -> dict:
	try:
		response = requests.post(f"{RCLONE_RC_URL}/{path}", json=payload or {}, timeout=20)
	except requests.RequestException as exc:
		raise HTTPException(status_code=502, detail=f"rclone RC is not reachable: {exc}") from exc
	if response.status_code >= 400:
		raise HTTPException(status_code=502, detail=f"rclone RC {path} failed with HTTP {response.status_code}: {response.text[:300]}")
	return response.json() if response.text else {}


def rclone_config_rc(path: str, payload: dict | None = None) -> dict:
	try:
		response = requests.post(f"{RCLONE_RC_URL}/{path}", json=payload or {}, timeout=30)
	except requests.RequestException as exc:
		raise HTTPException(status_code=502, detail=f"rclone RC is not reachable: {exc}") from exc
	if response.status_code >= 400:
		raise HTTPException(status_code=502, detail=f"rclone RC {path} failed; check the remote name, provider type, and required fields")
	return response.json() if response.text else {}


def validate_remote_name(name: str) -> str:
	name = name.strip().rstrip(":")
	if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", name):
		raise HTTPException(status_code=400, detail="Remote name may only contain letters, numbers, dots, underscores, and dashes")
	return name


def allowed_rclone_parameters(remote_type: str, parameters: dict) -> dict:
	remote_type = remote_type.strip().lower()
	allowed = {
		"s3": {
			"provider",
			"env_auth",
			"access_key_id",
			"secret_access_key",
			"region",
			"endpoint",
			"location_constraint",
			"acl",
			"server_side_encryption",
			"storage_class",
		},
		"webdav": {"url", "vendor", "user", "pass"},
		"local": {"nounc", "copy_links", "links"},
	}
	if remote_type not in allowed:
		raise HTTPException(status_code=400, detail="Only s3, webdav, and local rclone remotes can be created from this archive UI")
	clean = {}
	for key, value in parameters.items():
		if key in allowed[remote_type] and value not in {None, ""}:
			clean[key] = str(value) if not isinstance(value, bool) else value
	if remote_type == "s3":
		clean.setdefault("provider", "Other")
	if remote_type == "webdav":
		if not clean.get("url"):
			raise HTTPException(status_code=400, detail="WebDAV remotes require parameters.url")
		clean.setdefault("vendor", "other")
	return clean


def public_rclone_remotes() -> dict:
	remotes = rclone_config_rc("config/listremotes").get("remotes", [])
	managed = sorted(
		{
			event.get("name")
			for event in db.archive_backup_remote_events.find({"action": "create"}, {"_id": 0, "name": 1})
			if event.get("name")
		}
		- {
			event.get("name")
			for event in db.archive_backup_remote_events.find({"action": "delete"}, {"_id": 0, "name": 1})
			if event.get("name")
		}
	)
	return {"remotes": remotes, "managed_remotes": managed}


def destination_base(config: dict) -> str:
	remote_path = str(config.get("remote_path") or "").strip().strip("/")
	if remote_path:
		return remote_path
	remote = str(config.get("remote") or "").strip().rstrip(":")
	path = str(config.get("path") or config.get("prefix") or "discord-ro").strip().strip("/")
	if not remote:
		raise HTTPException(status_code=400, detail='rclone backup targets require config.remote or config.remote_path, for example {"remote":"gdrive","path":"discord-ro"}')
	if not re.match(r"^[A-Za-z0-9_.-]+$", remote):
		raise HTTPException(status_code=400, detail="rclone remote name contains unsupported characters")
	return f"{remote}:{path}" if path else f"{remote}:"


def join_remote_path(base: str, child: str) -> str:
	if base.endswith(":") or base.endswith("/"):
		return f"{base}{child}"
	return f"{base}/{child}"


def live_counts(code: str) -> dict:
	return {
		"channels": db.archive_live_channels.count_documents({"source_code": code}),
		"messages": db.archive_live_messages.count_documents({"source_code": code}),
		"last_message_at": (
			db.archive_live_messages.find_one({"source_code": code}, {"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)])
			or {}
		).get("timestamp"),
	}


def source_guild_id(source: dict) -> str | None:
	return ((source.get("metadata") or {}).get("guild") or {}).get("id")


def cached_message_collection(guild_id: str | None):
	if not guild_id:
		return None
	padded = str(guild_id).zfill(24)
	name = f"g{padded}_messages"
	if name not in db.list_collection_names():
		return None
	return db[name]


def public_cache_message(message: dict) -> dict:
	content = message.get("content")
	if isinstance(content, list):
		text = "\n".join(str(part.get("content", "")) for part in content if isinstance(part, dict) and part.get("content"))
	else:
		text = str(content or "")
	author = message.get("author") or {}
	return {
		"id": str(message.get("_id") or message.get("id") or ""),
		"timestamp": message.get("timestamp"),
		"channel_id": message.get("channelId"),
		"author": {
			"name": author.get("nickname") or author.get("name") or "Unknown author",
			"id": author.get("_id"),
		},
		"content": text,
		"source": "cache",
	}


def public_live_message(message: dict) -> dict:
	payload = message.get("payload") or {}
	author = payload.get("author") or {}
	return {
		"id": payload.get("id") or message.get("id"),
		"timestamp": payload.get("timestamp") or message.get("timestamp"),
		"channel_id": message.get("channel_id") or payload.get("channel_id"),
		"author": {
			"name": author.get("global_name") or author.get("username") or "Unknown author",
			"id": author.get("id"),
		},
		"content": payload.get("content") or "",
		"source": "live",
	}


def record_refresh_job(code: str, status: dict, trigger: str = "manual") -> dict:
	job = {
		"source_code": code,
		"trigger": trigger,
		"state": status.get("state"),
		"detail": status.get("detail"),
		"batch": status.get("batch"),
		"live_counts": status.get("live_counts"),
		"cache_policy": status.get("cache_policy"),
		"created_at": now_iso(),
	}
	db.archive_refresh_jobs.insert_one(dict(job))
	return job


def bot_auth_header() -> str | None:
	token = discord_config_value("bot_token")
	if not token:
		return None
	return f"Bot {token}"


def importer_token() -> str | None:
	return discord_config_value("importer_token") or discord_config_value("bot_token")


def normalize_exported_message(message: dict, channel_id: str | None) -> dict:
	author = message.get("author") or {}
	return {
		"id": str(message.get("id") or ""),
		"timestamp": message.get("timestamp") or message.get("timestampEdited"),
		"channel_id": channel_id or str(message.get("channelId") or ""),
		"author": {
			"id": str(author.get("id") or ""),
			"username": author.get("name") or author.get("username") or author.get("displayName") or "Unknown author",
			"global_name": author.get("displayName") or author.get("globalName") or author.get("name"),
		},
		"content": message.get("content") or "",
		"attachments": message.get("attachments") or [],
		"embeds": message.get("embeds") or [],
		"reactions": message.get("reactions") or [],
	}


def import_dce_json_file(path: Path, source: dict, guild_id: str, now: str) -> tuple[int, int]:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return 0, 0
	channel = payload.get("channel") or {}
	channel_id = str(channel.get("id") or "")
	if channel_id:
		db.archive_live_channels.update_one(
			{"source_code": source["code"], "id": channel_id},
			{
				"$set": {
					"source_code": source["code"],
					"guild_id": guild_id,
					"payload": channel,
					"updated_at": now,
					"source": "discordchatexporter",
				},
				"$setOnInsert": {"created_at": now},
			},
			upsert=True,
		)
	messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
	imported = 0
	for message in messages:
		if not isinstance(message, dict) or not message.get("id"):
			continue
		normalized = normalize_exported_message(message, channel_id)
		db.archive_live_messages.update_one(
			{"source_code": source["code"], "id": normalized["id"]},
			{
				"$set": {
					"source_code": source["code"],
					"guild_id": guild_id,
					"channel_id": normalized["channel_id"],
					"timestamp": normalized["timestamp"],
					"payload": normalized,
					"raw_export": message,
					"updated_at": now,
					"source": "discordchatexporter",
				},
				"$setOnInsert": {"created_at": now},
			},
			upsert=True,
		)
		imported += 1
	return (1 if channel_id else 0), imported


def refresh_with_discord_chat_exporter(source: dict, guild_id: str) -> dict | None:
	token = importer_token()
	if not token:
		return None
	if not Path(DCE_CLI_PATH).exists():
		return {
			"state": "exporter_unavailable",
			"detail": "DiscordChatExporter CLI is not installed in the app container; existing archive cache was left untouched.",
			"last_attempt_at": now_iso(),
			"last_success_at": source.get("live_refresh", {}).get("last_success_at"),
			"cache_policy": "read-through only; missing upstream data never deletes cached archive documents",
			"live_counts": live_counts(source["code"]),
		}

	now = now_iso()
	output_root = Path(DCE_LIVE_EXPORT_DIR)
	output_root.mkdir(parents=True, exist_ok=True)
	work_dir = Path(tempfile.mkdtemp(prefix=f"{source['code']}-", dir=str(output_root)))
	env = {**os.environ, "DISCORD_TOKEN": token}
	try:
		channels_result = subprocess.run(
			[DCE_CLI_PATH, "channels", "-g", str(guild_id)],
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
			check=False,
		)
	except subprocess.TimeoutExpired:
		shutil.rmtree(work_dir, ignore_errors=True)
		return {
			"state": "exporter_timeout",
			"detail": "DiscordChatExporter channel discovery timed out; existing archive cache was left untouched.",
			"last_attempt_at": now,
			"last_success_at": source.get("live_refresh", {}).get("last_success_at"),
			"cache_policy": "read-through only; missing upstream data never deletes cached archive documents",
			"live_counts": live_counts(source["code"]),
		}
	if channels_result.returncode != 0:
		shutil.rmtree(work_dir, ignore_errors=True)
		return {
			"state": "exporter_failed",
			"detail": f"DiscordChatExporter channel discovery exited with code {channels_result.returncode}; existing archive cache was left untouched.",
			"last_attempt_at": now,
			"last_success_at": source.get("live_refresh", {}).get("last_success_at"),
			"cache_policy": "read-through only; missing upstream data never deletes cached archive documents",
			"live_counts": live_counts(source["code"]),
		}

	discovered_channels = []
	for line in channels_result.stdout.splitlines():
		match = re.match(r"^\s*(\d{5,})\s+\|\s+(.+?)\s*$", line)
		if match:
			discovered_channels.append({"id": match.group(1), "name": match.group(2)})
	if not discovered_channels:
		shutil.rmtree(work_dir, ignore_errors=True)
		return {
			"state": "no_channels",
			"detail": "DiscordChatExporter authenticated but found no exportable channels; existing archive cache was left untouched.",
			"last_attempt_at": now,
			"last_success_at": source.get("live_refresh", {}).get("last_success_at"),
			"cache_policy": "read-through only; missing upstream data never deletes cached archive documents",
			"live_counts": live_counts(source["code"]),
			"batch": {"engine": "discordchatexporter", "channel_count": 0, "message_count": 0, "complete_cycle": True},
		}

	prior_refresh = source.get("live_refresh") or {}
	batch_cursor = int(prior_refresh.get("next_channel_index") or 0)
	if batch_cursor >= len(discovered_channels):
		batch_cursor = 0
	batch_channels = discovered_channels[batch_cursor:batch_cursor + MAX_LIVE_CHANNELS]
	next_channel_index = batch_cursor + len(batch_channels)
	if next_channel_index >= len(discovered_channels):
		next_channel_index = 0

	after = prior_refresh.get("last_success_at")
	if not after:
		after = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DCE_INITIAL_LOOKBACK_DAYS)).isoformat()
	channels = 0
	messages = 0
	failures = []
	for channel in batch_channels:
		channel_id = channel["id"]
		output_path = work_dir / f"{channel_id}.json"
		command = [
			DCE_CLI_PATH,
			"export",
			"-c",
			channel_id,
			"-f",
			"Json",
			"--after",
			after,
			"--include-threads",
			"all",
			"-o",
			str(output_path),
		]
		try:
			result = subprocess.run(
				command,
				env=env,
				text=True,
				capture_output=True,
				timeout=DCE_CHANNEL_TIMEOUT_SECONDS,
				check=False,
			)
		except subprocess.TimeoutExpired:
			failures.append({"channel_id": channel_id, "status": "timeout"})
			continue
		if result.returncode != 0:
			failures.append({"channel_id": channel_id, "status": f"exit_{result.returncode}"})
			continue
		channel_count, message_count = import_dce_json_file(output_path, source, guild_id, now)
		channels += channel_count
		messages += message_count
	shutil.rmtree(work_dir, ignore_errors=True)
	detail = f"DiscordChatExporter refreshed batch {batch_cursor + 1}-{batch_cursor + len(batch_channels)} of {len(discovered_channels)} channel(s), importing {messages} message(s) from {channels} channel export(s). Existing exported cache remains the fallback."
	if failures:
		detail += f" {len(failures)} channel export(s) were unavailable, empty, or timed out."
	return {
		"state": "fresh" if messages else "no_new_live_messages",
		"detail": detail,
		"last_attempt_at": now,
		"last_success_at": now if messages else source.get("live_refresh", {}).get("last_success_at"),
		"cache_policy": "append/update only; missing upstream data never deletes cached archive documents",
		"live_counts": live_counts(source["code"]),
		"failures": failures[:10],
		"batch": {
			"engine": "discordchatexporter",
			"channel_count": len(discovered_channels),
			"batch_size": MAX_LIVE_CHANNELS,
			"started_at_index": batch_cursor,
			"next_channel_index": next_channel_index,
			"message_count": messages,
			"complete_cycle": next_channel_index == 0,
		},
		"next_channel_index": next_channel_index,
	}


def refresh_with_bot(source: dict, guild_id: str) -> dict:
	auth_header = bot_auth_header()
	if not auth_header:
		raise HTTPException(status_code=400, detail="Discord bot token is not configured")
	status, channels_payload = discord_get(f"/guilds/{guild_id}/channels", auth_header)
	if status in {401, 403, 404}:
		return {
			"state": "upstream_unavailable",
			"detail": f"Discord channel lookup returned HTTP {status}; existing archive cache was left untouched.",
			"last_attempt_at": now_iso(),
			"last_success_at": source.get("live_refresh", {}).get("last_success_at"),
			"cache_policy": "read-through only; missing upstream data never deletes cached archive documents",
		}
	if status >= 400 or not isinstance(channels_payload, list):
		raise HTTPException(status_code=502, detail=f"Discord channel lookup failed with HTTP {status}")

	code = source["code"]
	now = now_iso()
	prior_refresh = source.get("live_refresh") or {}
	batch_cursor = int(prior_refresh.get("next_channel_index") or 0)
	text_channels = []
	for channel in channels_payload:
		if channel.get("type") in {0, 5, 10, 11, 12, 15}:
			text_channels.append(channel)
		db.archive_live_channels.update_one(
			{"source_code": code, "id": channel.get("id")},
			{"$set": {"source_code": code, "guild_id": guild_id, "payload": channel, "updated_at": now}, "$setOnInsert": {"created_at": now}},
			upsert=True,
		)

	if batch_cursor >= len(text_channels):
		batch_cursor = 0
	batch_channels = text_channels[batch_cursor:batch_cursor + MAX_LIVE_CHANNELS]
	next_channel_index = batch_cursor + len(batch_channels)
	if next_channel_index >= len(text_channels):
		next_channel_index = 0

	fetched_channels = 0
	fetched_messages = 0
	failures = []
	for channel in batch_channels:
		channel_id = channel.get("id")
		if not channel_id:
			continue
		status, messages_payload = discord_get(
			f"/channels/{channel_id}/messages",
			auth_header,
			params={"limit": min(MAX_LIVE_MESSAGES_PER_CHANNEL, 100)},
		)
		if status in {401, 403, 404}:
			failures.append({"channel_id": channel_id, "status": status})
			continue
		if status >= 400 or not isinstance(messages_payload, list):
			failures.append({"channel_id": channel_id, "status": status})
			continue
		fetched_channels += 1
		for message in messages_payload:
			message_id = message.get("id")
			if not message_id:
				continue
			db.archive_live_messages.update_one(
				{"source_code": code, "id": message_id},
				{
					"$set": {
						"source_code": code,
						"guild_id": guild_id,
						"channel_id": channel_id,
						"timestamp": message.get("timestamp"),
						"payload": message,
						"updated_at": now,
					},
					"$setOnInsert": {"created_at": now},
				},
				upsert=True,
			)
			fetched_messages += 1

	state = "fresh" if fetched_messages else "no_new_live_messages"
	detail = f"Batch refreshed channels {batch_cursor + 1}-{batch_cursor + len(batch_channels)} of {len(text_channels)}; fetched {fetched_messages} recent messages from {fetched_channels} channel(s). Existing exported cache remains the fallback."
	if failures:
		detail += f" {len(failures)} channel(s) were unavailable or unauthorized."
	return {
		"state": state,
		"detail": detail,
		"last_attempt_at": now,
		"last_success_at": now if fetched_messages else source.get("live_refresh", {}).get("last_success_at"),
		"cache_policy": "append/update only; missing upstream data never deletes cached archive documents",
		"live_counts": live_counts(code),
		"failures": failures[:10],
		"batch": {
			"channel_count": len(text_channels),
			"batch_size": MAX_LIVE_CHANNELS,
			"started_at_index": batch_cursor,
			"next_channel_index": next_channel_index,
			"complete_cycle": next_channel_index == 0,
		},
		"next_channel_index": next_channel_index,
	}


@router.get("/sources")
def list_sources():
	return list(db.archive_sources.find({"enabled": True}, {"_id": 0}).sort([("created_at", 1)]))


@router.post("/sources")
def add_source(source: InviteSourceRequest):
	code = extract_invite_code(source.invite)
	metadata = fetch_invite_metadata(code)
	document = {
		"code": code,
		"invite": source.invite,
		"label": source.label,
		"metadata": metadata,
		"enabled": True,
		"created_at": now_iso(),
		"updated_at": now_iso(),
		"live_refresh": {
			"state": "needs_authorization",
			"detail": "Anonymous invite lookup only proves the server/channel metadata. Message refresh requires a Discord OAuth or bot token with access to the server.",
			"last_attempt_at": None,
			"last_success_at": None,
		},
	}
	db.archive_sources.update_one(
		{"code": code},
		{
			"$set": {
				"invite": document["invite"],
				"label": document["label"],
				"metadata": document["metadata"],
				"enabled": True,
				"updated_at": document["updated_at"],
				"live_refresh": document["live_refresh"],
			},
			"$unset": {"removed_at": ""},
			"$setOnInsert": {
				"code": code,
				"created_at": document["created_at"],
			},
		},
		upsert=True,
	)
	return db.archive_sources.find_one({"code": code}, {"_id": 0})


@router.delete("/sources/{code}")
def remove_source(code: str):
	code = extract_invite_code(code)
	result = db.archive_sources.update_one(
		{"code": code},
		{"$set": {"enabled": False, "removed_at": now_iso(), "updated_at": now_iso()}},
	)
	if result.matched_count == 0:
		raise HTTPException(status_code=404, detail="Archive source not found")
	return {"code": code, "enabled": False}


@router.post("/sources/{code}/refresh")
def refresh_source(code: str):
	code = extract_invite_code(code)
	source = db.archive_sources.find_one({"code": code})
	if source is None:
		raise HTTPException(status_code=404, detail="Archive source not found")

	status = {
		"state": "metadata_refreshed_waiting_authorization",
		"detail": "Invite metadata can refresh anonymously. Live message batches will run when Discord OAuth or bot authorization is connected; existing archive cache is served until then.",
		"last_attempt_at": now_iso(),
		"last_success_at": source.get("live_refresh", {}).get("last_success_at"),
		"cache_policy": "read-through only; missing upstream data never deletes cached archive documents",
		"live_counts": live_counts(code),
	}
	try:
		metadata = fetch_invite_metadata(code)
		db.archive_sources.update_one({"code": code}, {"$set": {"metadata": metadata, "updated_at": now_iso()}})
		source["metadata"] = metadata
	except HTTPException as exc:
		status["invite_metadata_error"] = exc.detail

	guild_id = ((source.get("metadata") or {}).get("guild") or {}).get("id")
	exporter_status = refresh_with_discord_chat_exporter(source, guild_id) if guild_id else None
	if exporter_status and exporter_status.get("state") not in {"exporter_unavailable", "exporter_failed", "exporter_timeout"}:
		status = exporter_status
	elif bot_auth_header() and guild_id:
		status = refresh_with_bot(source, guild_id)
	elif exporter_status:
		status = exporter_status
	elif latest_oauth_token() and guild_id:
		token_doc = latest_oauth_token()
		status["state"] = "needs_bot_or_importer"
		status["detail"] = "Discord OAuth is authorized for login/guild awareness. Message export uses DiscordChatExporter or a bot token when connected; existing archive cache is served."
		status["authorized_guild"] = guild_id in set(token_doc.get("guild_ids") or [])

	db.archive_sources.update_one({"code": code}, {"$set": {"live_refresh": status, "updated_at": now_iso()}})
	job = record_refresh_job(code, status)
	return {"code": code, "refresh": status, "job": job}


@router.post("/refresh/run")
def refresh_all_sources():
	results = []
	for source in db.archive_sources.find({"enabled": True}, {"_id": 0}).sort([("created_at", 1)]):
		try:
			results.append(refresh_source(source["code"]))
		except HTTPException as exc:
			status = {
				"state": "refresh_failed_cache_preserved",
				"detail": exc.detail,
				"last_attempt_at": now_iso(),
				"cache_policy": "failed upstream reads never delete cached archive documents",
				"live_counts": live_counts(source["code"]),
			}
			db.archive_sources.update_one({"code": source["code"]}, {"$set": {"live_refresh": status, "updated_at": now_iso()}})
			results.append({"code": source["code"], "refresh": status, "job": record_refresh_job(source["code"], status, trigger="refresh_all")})
	return {"count": len(results), "results": results}


@router.get("/refresh/jobs")
def list_refresh_jobs():
	return list(db.archive_refresh_jobs.find({}, {"_id": 0}).sort([("created_at", -1)]).limit(30))


@router.get("/refresh/status")
def refresh_status():
	return {
		"sources": list(db.archive_sources.find({"enabled": True}, {"_id": 0, "code": 1, "enabled": 1, "metadata.guild.name": 1, "live_refresh": 1}).sort([("created_at", 1)])),
		"cache_policy": "The archive is append/update only. Failed upstream reads and missing upstream messages must not clear Mongo or export-cache data.",
		"authorization": {
			"discord_oauth_configured": discord_oauth_configured(),
			"discord_oauth_client_id_present": bool(discord_config_value("client_id")),
			"discord_oauth_token_present": latest_oauth_token() is not None,
			"discord_bot_token_configured": bool(discord_config_value("bot_token")),
			"discord_importer_token_configured": bool(discord_config_value("importer_token")),
			"discord_chat_exporter_available": Path(DCE_CLI_PATH).exists(),
			"discord_oauth_start_path": "/api/archive/discord/oauth/start",
			"config": public_discord_config(),
		},
	}


@router.get("/discord/oauth/start")
def start_discord_oauth():
	client_id = discord_config_value("client_id")
	if not client_id or not discord_config_value("client_secret"):
		raise HTTPException(
			status_code=409,
			detail="Discord OAuth client settings are not connected yet. Save them in Archive Controls or provide env vars, then authorize.",
		)
	state = secrets.token_urlsafe(24)
	db.archive_oauth_states.insert_one({"state": state, "provider": "discord", "created_at": now_iso()})
	query = urlencode(
		{
			"client_id": client_id,
			"redirect_uri": discord_redirect_uri(),
			"response_type": "code",
			"scope": "identify guilds",
			"state": state,
			"prompt": "consent",
		}
	)
	return RedirectResponse(f"{DISCORD_AUTHORIZE_URL}?{query}", status_code=302)


@router.get("/discord/oauth/callback")
def discord_oauth_callback(code: str | None = None, state: str | None = None):
	if not code or not state:
		raise HTTPException(status_code=400, detail="Discord OAuth callback is missing code or state")
	state_doc = db.archive_oauth_states.find_one({"state": state, "provider": "discord"})
	if state_doc is None:
		raise HTTPException(status_code=400, detail="Discord OAuth state is invalid or expired")
	if not discord_oauth_configured():
		raise HTTPException(status_code=503, detail="Discord OAuth is not configured for token exchange")
	token_payload = exchange_discord_code(code)
	public_token = store_oauth_token(token_payload)
	db.archive_oauth_states.update_one({"_id": state_doc["_id"]}, {"$set": {"used_at": now_iso()}})
	return HTMLResponse(
		"""
		<!doctype html><meta charset="utf-8">
		<title>Discord authorization complete</title>
		<body style="font-family: system-ui; background:#10151c; color:#e8edf2">
		<h1>Discord authorization complete</h1>
		<p>You can close this page and use Refresh in the archive controls.</p>
		<script>setTimeout(() => { location.href = "/"; }, 1800)</script>
		</body>
		"""
	)


@router.get("/discord/oauth/status")
def discord_oauth_status():
	token_doc = latest_oauth_token()
	if token_doc is None:
		return {"authorized": False}
	return {
		"authorized": True,
		"user": token_doc.get("user"),
		"scope": token_doc.get("scope"),
		"expires_at": token_doc.get("expires_at"),
		"guild_count": token_doc.get("guild_count", 0),
	}


@router.get("/discord/config")
def get_discord_config():
	return public_discord_config()


@router.post("/discord/config")
def save_discord_config(config: DiscordConfigRequest):
	existing = stored_discord_config()
	updates = {}
	for key in ("client_id", "client_secret", "redirect_uri", "bot_token", "importer_token"):
		value = getattr(config, key)
		if value is not None:
			updates[key] = value.strip()
	document = {**existing, **updates, "updated_at": now_iso()}
	db.archive_settings.update_one({"key": "discord_config"}, {"$set": {"key": "discord_config", **document}}, upsert=True)
	return public_discord_config()


@router.get("/sources/{code}/live-messages")
def list_live_messages(code: str, limit: int = 50):
	code = extract_invite_code(code)
	limit = max(1, min(limit, 100))
	return list(
		db.archive_live_messages.find({"source_code": code}, {"_id": 0})
		.sort([("timestamp", -1)])
		.limit(limit)
	)


@router.get("/sources/{code}/messages")
def list_source_messages(code: str, limit: int = 50):
	code = extract_invite_code(code)
	limit = max(1, min(limit, 100))
	source = db.archive_sources.find_one({"code": code}, {"_id": 0})
	if source is None:
		raise HTTPException(status_code=404, detail="Archive source not found")

	live_messages = list(
		db.archive_live_messages.find({"source_code": code}, {"_id": 0})
		.sort([("timestamp", -1)])
		.limit(limit)
	)
	if live_messages:
		return {
			"mode": "live",
			"detail": "Served live messages fetched from Discord. Cached/exported archive remains available as fallback.",
			"messages": [public_live_message(message) for message in live_messages],
			"cache_policy": "live reads never delete cached archive documents",
		}

	collection = cached_message_collection(source_guild_id(source))
	if collection is None:
		return {
			"mode": "empty",
			"detail": "No live messages are available and no exported cache collection exists for this invite guild.",
			"messages": [],
			"cache_policy": "missing upstream/cache data did not clear any archive data",
		}
	cache_messages = list(
		collection.find({}, {"_id": 1, "timestamp": 1, "channelId": 1, "author": 1, "content": 1})
		.sort([("timestamp", -1)])
		.limit(limit)
	)
	return {
		"mode": "cache",
		"detail": "Served exported cache because no live messages are currently available for this source.",
		"messages": [public_cache_message(message) for message in cache_messages],
		"cache_policy": "fallback reads are read-only and never clear archive data",
	}


@router.get("/backup-targets")
def list_backup_targets():
	return list(db.archive_backup_targets.find({}, {"_id": 0}).sort([("name", 1)]))


@router.post("/backup-targets")
def upsert_backup_target(target: BackupTargetRequest):
	document = target.model_dump() if hasattr(target, "model_dump") else target.dict()
	document["updated_at"] = now_iso()
	db.archive_backup_targets.update_one(
		{"name": target.name},
		{"$set": document, "$setOnInsert": {"created_at": now_iso()}},
		upsert=True,
	)
	return db.archive_backup_targets.find_one({"name": target.name}, {"_id": 0})


@router.delete("/backup-targets/{name}")
def delete_backup_target(name: str):
	result = db.archive_backup_targets.delete_one({"name": name})
	if result.deleted_count == 0:
		raise HTTPException(status_code=404, detail="Backup target not found")
	return {"name": name, "deleted": True}


@router.get("/backup-remotes")
def list_backup_remotes():
	return public_rclone_remotes()


@router.post("/backup-remotes")
def create_backup_remote(remote: RcloneRemoteRequest):
	name = validate_remote_name(remote.name)
	remote_type = remote.type.strip().lower()
	parameters = allowed_rclone_parameters(remote_type, remote.parameters or {})
	rclone_config_rc(
		"config/create",
		{
			"name": name,
			"type": remote_type,
			"parameters": parameters,
			"opt": {
				"obscure": True,
				"nonInteractive": True,
				"noOutput": True,
			},
		},
	)
	db.archive_backup_remote_events.insert_one(
		{
			"name": name,
			"type": remote_type,
			"action": "create",
			"created_at": now_iso(),
			"parameter_keys": sorted(parameters.keys()),
		}
	)
	return {"name": name, "type": remote_type, "created": True, "remotes": public_rclone_remotes().get("remotes", [])}


@router.delete("/backup-remotes/{name}")
def delete_backup_remote(name: str):
	name = validate_remote_name(name)
	created = db.archive_backup_remote_events.count_documents({"name": name, "action": "create"})
	deleted = db.archive_backup_remote_events.count_documents({"name": name, "action": "delete"})
	if created <= deleted:
		raise HTTPException(status_code=403, detail="Only remotes created through Archive Controls can be deleted here")
	rclone_config_rc("config/delete", {"name": name})
	db.archive_backup_remote_events.insert_one({"name": name, "action": "delete", "deleted_at": now_iso()})
	return {"name": name, "deleted": True, "remotes": public_rclone_remotes().get("remotes", [])}


@router.post("/backup-targets/{name}/run")
def run_backup_target(name: str, request: BackupRunRequest):
	if request.source not in {"all", "exports", "cache"}:
		raise HTTPException(status_code=400, detail="Backup source must be one of all, exports, or cache")
	target = db.archive_backup_targets.find_one({"name": name}, {"_id": 0})
	if target is None:
		raise HTTPException(status_code=404, detail="Backup target not found")
	if not target.get("enabled", True):
		raise HTTPException(status_code=400, detail="Backup target is disabled")
	if target.get("provider") != "rclone":
		raise HTTPException(status_code=400, detail="Only rclone backup targets can be run by this deployment")

	base = destination_base(target.get("config") or {})
	source_names = list(BACKUP_SOURCES.keys()) if request.source == "all" else [request.source]
	jobs = []
	for source_name in source_names:
		payload = {
			"srcFs": BACKUP_SOURCES[source_name],
			"dstFs": join_remote_path(base, source_name),
			"createEmptySrcDirs": True,
			"_async": True,
			"_group": f"discord-ro-backup-{name}",
		}
		if request.dry_run:
			payload["_config"] = {"DryRun": True}
		result = rclone_rc("sync/copy", payload)
		jobs.append({"source": source_name, "jobid": result.get("jobid"), "dstFs": payload["dstFs"]})

	record = {
		"target": name,
		"provider": "rclone",
		"requested_source": request.source,
		"dry_run": request.dry_run,
		"jobs": jobs,
		"created_at": now_iso(),
		"state": "running",
		"cache_policy": "rclone copy is append/update only for backup targets; it does not delete local archive cache or destination-only backup objects.",
	}
	stored_record = dict(record)
	db.archive_backup_jobs.insert_one(stored_record)
	db.archive_backup_targets.update_one({"name": name}, {"$set": {"last_run": record, "updated_at": now_iso()}})
	return record


@router.get("/backup-jobs")
def list_backup_jobs():
	jobs = list(db.archive_backup_jobs.find({}, {"_id": 0}).sort([("created_at", -1)]).limit(20))
	for job in jobs:
		for child in job.get("jobs", []):
			if child.get("jobid") is not None:
				try:
					child["rclone_status"] = rclone_rc("job/status", {"jobid": child["jobid"]})
				except HTTPException as exc:
					child["rclone_status"] = {"error": exc.detail}
	return jobs
