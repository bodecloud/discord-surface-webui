import pymongo
from fastapi import APIRouter, Request

from ..common.Database import DM_GUILD_ID, Database

router = APIRouter(
	prefix="",
	tags=["guilds"]
)

@router.get("/guilds")
async def get_guilds(request: Request):
	"""
	Returns a list of guilds
	If allowlist is enabled (by not being an empty list), only allowlisted guilds will be returned.

	all other allowlist logic is handled by get_guild_collection() method - it won't return a collection for non-allowlisted guilds
	"""
	collection_guilds = Database.get_global_collection("guilds")
	allowlisted_guild_ids = Database.get_allowlisted_guild_ids()

	if len(allowlisted_guild_ids) == 0:
		query = {}
		if not Database.request_has_dm_access(request):
			query["_id"] = {"$ne": DM_GUILD_ID}
		cursor = collection_guilds.find(query).sort([("msg_count", pymongo.DESCENDING)])
	else:
		query = {
				"_id": {
					"$in": allowlisted_guild_ids
				}
			}
		if not Database.request_has_dm_access(request):
			query["_id"]["$ne"] = DM_GUILD_ID
		cursor = collection_guilds.find(query).sort([("msg_count", pymongo.DESCENDING)])
	return list(cursor)
