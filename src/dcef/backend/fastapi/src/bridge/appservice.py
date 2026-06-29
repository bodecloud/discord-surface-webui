import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request

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


def configured_hs_token() -> str | None:
	for key in TOKEN_ENV_KEYS:
		value = os.getenv(key)
		if value:
			return value
	return None


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
