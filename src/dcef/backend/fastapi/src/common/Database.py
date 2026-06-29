import pymongo
from fastapi import HTTPException, Request

URI = "mongodb://127.0.0.1:27017"
client = pymongo.MongoClient(URI)
db = client["dcef"]
collection_guilds = db["guilds"]
collection_config = db["config"]
DM_GUILD_ID = "000000000000000000000000"

def pad_id(id):
	if id == None:
		return None
	return str(id).zfill(24)

class Database:
	@staticmethod
	def is_online():
		try:
			client.server_info()
			return True
		except:
			return False

	@staticmethod
	def get_global_collection(collection_name):
		return db[collection_name]

	@staticmethod
	def get_allowlisted_guild_ids():
		allowlisted_guild_ids = collection_config.find_one({"key": "allowlisted_guild_ids"})["value"]
		allowlisted_guild_ids = [pad_id(id) for id in allowlisted_guild_ids]
		return allowlisted_guild_ids


	@staticmethod
	def get_denylisted_user_ids():
		denylisted_user_ids = collection_config.find_one({"key": "denylisted_user_ids"})["value"]
		denylisted_user_ids = [pad_id(id) for id in denylisted_user_ids]
		return denylisted_user_ids

	@staticmethod
	def get_guild_collection(guild_id, collection_name):
		allowlisted_guild_ids = Database.get_allowlisted_guild_ids()
		padded_guild_id = pad_id(guild_id)
		if len(allowlisted_guild_ids) > 0 and padded_guild_id not in allowlisted_guild_ids:
			raise Exception(f"Guild {guild_id} not allowlisted")

		return db[f"g{padded_guild_id}_{collection_name}"]

	@staticmethod
	def is_dm_guild(guild_id):
		return pad_id(guild_id) == DM_GUILD_ID

	@staticmethod
	def request_has_dm_access(request: Request):
		return request.headers.get("x-dm-authorized", "").lower() == "true"

	@staticmethod
	def require_dm_access(guild_id, request: Request):
		if Database.is_dm_guild(guild_id) and not Database.request_has_dm_access(request):
			raise HTTPException(status_code=403, detail="Direct Messages require DM-specific authorization")
