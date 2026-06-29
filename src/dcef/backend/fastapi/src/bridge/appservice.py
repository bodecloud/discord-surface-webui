import os
import re
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..common.Database import db


def add_preprocess_path():
	for parent in Path(__file__).resolve().parents:
		candidate = parent / "preprocess"
		if (candidate / "MatrixProcessor.py").exists():
			sys.path.insert(0, str(candidate))
			return
		candidate = parent / "backend" / "preprocess"
		if (candidate / "MatrixProcessor.py").exists():
			sys.path.insert(0, str(candidate))
			return
	env_path = Path(os.getenv("DCEF_PREPROCESS_PATH", "/dcef/backend/preprocess"))
	if (env_path / "MatrixProcessor.py").exists():
		sys.path.insert(0, str(env_path))


add_preprocess_path()

from MatrixProcessor import MATRIX_GUILD_ID, MatrixProcessor  # noqa: E402


router = APIRouter(prefix="", tags=["matrix-appservice"])

TXN_ID_RE = re.compile(r"^[A-Za-z0-9_.:=@~-]{1,256}$")
TOKEN_ENV_KEYS = ("MATRIX_APPSERVICE_HS_TOKEN", "MATRIX_HS_TOKEN")
AS_TOKEN_ENV_KEYS = ("MATRIX_APPSERVICE_AS_TOKEN", "MATRIX_AS_TOKEN")
SERVER_NAME_ENV_KEYS = ("MATRIX_SERVER_NAME", "MATRIX_HOMESERVER_NAME")
USER_PREFIX_ENV_KEYS = ("MATRIX_APPSERVICE_USER_PREFIX",)
ALIAS_PREFIX_ENV_KEYS = ("MATRIX_APPSERVICE_ALIAS_PREFIX",)
REGISTRATION_ID_ENV_KEYS = ("MATRIX_APPSERVICE_ID",)
SENDER_LOCALPART_ENV_KEYS = ("MATRIX_APPSERVICE_SENDER_LOCALPART",)

MATRIX_USER_ID_RE = re.compile(r"^@[^:\s]+:[^:\s]+$")
MATRIX_ROOM_ALIAS_RE = re.compile(r"^#[^:\s]+:[^:\s]+$")


class FastApiMongoDatabase:
	def get_guild_collections(self, guild_id):
		padded = str(guild_id).zfill(24)
		return {
			"messages": db[f"g{padded}_messages"],
			"channels": db[f"g{padded}_channels"],
			"authors": db[f"g{padded}_authors"],
			"emojis": db[f"g{padded}_emojis"],
			"assets": db[f"g{padded}_assets"],
			"roles": db[f"g{padded}_roles"],
			"guilds": db["guilds"],
			"jsons": db["jsons"],
			"config": db["config"],
		}

	def create_indexes(self, guild_id):
		self.get_guild_collections(guild_id)["messages"].create_index("channelId", default_language="none")


class NoopFileFinder:
	def add_base_directory(self, path):
		return path


class NoopAssetProcessor:
	def set_guild_id(self, guild_id):
		self.guild_id = guild_id


def env_first(keys: tuple[str, ...], fallback: str | None = None) -> str | None:
	for key in keys:
		value = os.getenv(key)
		if value:
			return value
	return fallback


def appservice_settings(generate: bool = False) -> dict:
	config = db["config"]
	settings = config.find_one({"key": "matrix_appservice"}) or {}
	value = settings.get("value") if isinstance(settings.get("value"), dict) else {}
	changed = False
	if generate and not value.get("as_token"):
		value["as_token"] = secrets.token_hex(32)
		changed = True
	if generate and not value.get("hs_token"):
		value["hs_token"] = secrets.token_hex(32)
		changed = True
	if changed:
		config.update_one({"key": "matrix_appservice"}, {"$set": {"key": "matrix_appservice", "value": value}}, upsert=True)
	return value


def configured_hs_token(generate: bool = False) -> str | None:
	return env_first(TOKEN_ENV_KEYS) or appservice_settings(generate).get("hs_token")


def configured_as_token(generate: bool = False) -> str | None:
	return env_first(AS_TOKEN_ENV_KEYS) or appservice_settings(generate).get("as_token")


def configured_server_name() -> str:
	return env_first(SERVER_NAME_ENV_KEYS, "example.org")


def configured_user_prefix() -> str:
	return env_first(USER_PREFIX_ENV_KEYS, "_discord_")


def configured_alias_prefix() -> str:
	return env_first(ALIAS_PREFIX_ENV_KEYS, "_discord_")


def configured_registration_id() -> str:
	return env_first(REGISTRATION_ID_ENV_KEYS, "discord-surface-webui")


def configured_sender_localpart() -> str:
	return env_first(SENDER_LOCALPART_ENV_KEYS, "_discord_bridge")


def public_base_url(request: Request) -> str:
	configured = os.getenv("MATRIX_APPSERVICE_URL")
	if configured:
		return configured.rstrip("/")
	return str(request.base_url).rstrip("/").removesuffix("/api")


def bearer_token(authorization: str | None) -> str | None:
	if not authorization:
		return None
	kind, _, token = authorization.partition(" ")
	if kind.lower() != "bearer" or not token:
		return None
	return token.strip()


def require_appservice_auth(authorization: str | None, access_token: str | None):
	expected = configured_hs_token()
	if not expected:
		return
	header_token = bearer_token(authorization)
	if header_token != expected:
		raise HTTPException(status_code=403, detail={"errcode": "M_FORBIDDEN", "error": "Invalid application service token"})
	if access_token is not None and access_token != expected:
		raise HTTPException(status_code=403, detail={"errcode": "M_FORBIDDEN", "error": "Mismatched legacy access_token"})


def transaction_collection():
	return db["bridge_matrix_transactions"]


def regex_escape(value: str) -> str:
	return re.escape(value)


def registration_document(request: Request) -> dict:
	user_prefix = configured_user_prefix()
	alias_prefix = configured_alias_prefix()
	return {
		"id": configured_registration_id(),
		"url": public_base_url(request) + "/api",
		"as_token": configured_as_token(generate=True),
		"hs_token": configured_hs_token(generate=True),
		"sender_localpart": configured_sender_localpart(),
		"rate_limited": False,
		"push_ephemeral": True,
		"namespaces": {
			"users": [{
				"exclusive": True,
				"regex": f"@{regex_escape(user_prefix)}.*:{regex_escape(configured_server_name())}",
			}],
			"aliases": [{
				"exclusive": True,
				"regex": f"#{regex_escape(alias_prefix)}.*:{regex_escape(configured_server_name())}",
			}],
			"rooms": [],
		},
		"protocols": ["discord"],
	}


def yaml_scalar(value):
	if isinstance(value, bool):
		return "true" if value else "false"
	if value is None:
		return "null"
	return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_lines(value, indent: int = 0) -> list[str]:
	pad = " " * indent
	if isinstance(value, dict):
		lines = []
		for key, item in value.items():
			if isinstance(item, (dict, list)):
				lines.append(f"{pad}{key}:")
				lines.extend(yaml_lines(item, indent + 2))
			else:
				lines.append(f"{pad}{key}: {yaml_scalar(item)}")
		return lines
	if isinstance(value, list):
		if not value:
			return [f"{pad}[]"]
		lines = []
		for item in value:
			if isinstance(item, dict):
				lines.append(f"{pad}-")
				lines.extend(yaml_lines(item, indent + 2))
			elif isinstance(item, list):
				lines.append(f"{pad}-")
				lines.extend(yaml_lines(item, indent + 2))
			else:
				lines.append(f"{pad}- {yaml_scalar(item)}")
		return lines
	return [f"{pad}{yaml_scalar(value)}"]


def registration_yaml(request: Request) -> str:
	return "\n".join(yaml_lines(registration_document(request))) + "\n"


def user_in_namespace(user_id: str) -> bool:
	server_name = configured_server_name()
	return bool(MATRIX_USER_ID_RE.match(user_id)) and user_id.startswith(f"@{configured_user_prefix()}") and user_id.endswith(f":{server_name}")


def alias_in_namespace(room_alias: str) -> bool:
	server_name = configured_server_name()
	return bool(MATRIX_ROOM_ALIAS_RE.match(room_alias)) and room_alias.startswith(f"#{configured_alias_prefix()}") and room_alias.endswith(f":{server_name}")


def process_transaction_payload(txn_id: str, payload: dict) -> int:
	processor = MatrixProcessor(
		FastApiMongoDatabase(),
		NoopFileFinder(),
		f"matrix-appservice/{txn_id}.json",
		NoopAssetProcessor(),
		1,
		1,
	)
	processor.process_payload(payload, mark_processed=False)
	return len(MatrixProcessor.extract_events(payload))


async def handle_transaction(
	txn_id: str,
	request: Request,
	authorization: str | None,
	access_token: str | None,
):
	if TXN_ID_RE.match(txn_id) is None:
		raise HTTPException(status_code=422, detail="transaction id contains unsupported characters")
	require_appservice_auth(authorization, access_token)
	payload = await request.json()
	if not isinstance(payload, dict):
		raise HTTPException(status_code=400, detail="transaction body must be a JSON object")
	transactions = transaction_collection()
	existing = transactions.find_one({"_id": txn_id}, {"_id": 1, "event_count": 1})
	if existing is not None:
		return {"processed": existing.get("event_count", 0), "duplicate": True}
	event_count = process_transaction_payload(txn_id, payload)
	transactions.insert_one({
		"_id": txn_id,
		"event_count": event_count,
		"processed_at": datetime.now(timezone.utc).isoformat(),
		"source": "matrix-appservice",
		"matrix_guild_id": MATRIX_GUILD_ID,
	})
	return {"processed": event_count, "duplicate": False}


@router.put("/_matrix/app/v1/transactions/{txn_id}")
async def put_matrix_appservice_transaction(
	txn_id: str,
	request: Request,
	authorization: str | None = Header(default=None),
	access_token: str | None = Query(default=None),
):
	return await handle_transaction(txn_id, request, authorization, access_token)


@router.put("/transactions/{txn_id}")
async def put_legacy_matrix_appservice_transaction(
	txn_id: str,
	request: Request,
	authorization: str | None = Header(default=None),
	access_token: str | None = Query(default=None),
):
	return await handle_transaction(txn_id, request, authorization, access_token)


@router.get("/bridge/appservice/registration")
async def get_matrix_appservice_registration(request: Request, format: str = Query(default="yaml")):
	if format == "json":
		return registration_document(request)
	if format != "yaml":
		raise HTTPException(status_code=422, detail="format must be yaml or json")
	return PlainTextResponse(registration_yaml(request), media_type="application/yaml")


@router.get("/_matrix/app/v1/users/{user_id:path}")
async def query_matrix_appservice_user(user_id: str, authorization: str | None = Header(default=None), access_token: str | None = Query(default=None)):
	require_appservice_auth(authorization, access_token)
	user_id = urllib.parse.unquote(user_id)
	if not user_in_namespace(user_id):
		raise HTTPException(status_code=404, detail={"errcode": "M_NOT_FOUND", "error": "User is outside this bridge namespace"})
	return {}


@router.get("/users/{user_id:path}")
async def query_legacy_matrix_appservice_user(user_id: str, authorization: str | None = Header(default=None), access_token: str | None = Query(default=None)):
	return await query_matrix_appservice_user(user_id, authorization, access_token)


@router.get("/_matrix/app/v1/rooms/{room_alias:path}")
async def query_matrix_appservice_room(room_alias: str, authorization: str | None = Header(default=None), access_token: str | None = Query(default=None)):
	require_appservice_auth(authorization, access_token)
	room_alias = urllib.parse.unquote(room_alias)
	if not alias_in_namespace(room_alias):
		raise HTTPException(status_code=404, detail={"errcode": "M_NOT_FOUND", "error": "Room alias is outside this bridge namespace"})
	return {}


@router.get("/rooms/{room_alias:path}")
async def query_legacy_matrix_appservice_room(room_alias: str, authorization: str | None = Header(default=None), access_token: str | None = Query(default=None)):
	return await query_matrix_appservice_room(room_alias, authorization, access_token)
