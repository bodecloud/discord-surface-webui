from fastapi import APIRouter, Request

from ..common.Database import Database

router = APIRouter(
	prefix="",
	tags=["guild"]
)


@router.get("/guild/channels")
async def get_channels(guild_id: str, request: Request):
	"""
	Returns a list of all channels in a guild.
	That includes channels, threads and forum posts.
	"""
	Database.require_dm_access(guild_id, request)
	collection_channels = Database.get_guild_collection(guild_id, "channels")
	cursor = collection_channels.find()
	return list(cursor)
