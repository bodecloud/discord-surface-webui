import functools
import glob
import json
import re
import os

print = functools.partial(print, flush=True)


class FileFinder():
	"""
	Find all files in a directory
	"""
	def __init__(self, directory: str):
		self.base_directory = self.normalize_path(directory)

	def find_channel_exports(self):
		print("finding channel exports in " + self.base_directory)
		directory = self.base_directory
		files = []
		for filename in glob.glob(directory + '**/*.json', recursive=True, include_hidden=True):
			if filename.endswith('.json'):
				# ignore attachment files - they are made by users, not DiscordChatExporter
				if re.search(r"-([a-fA-F0-9]{5}|[a-f0-9]{16})\.json$", filename) != None:
					continue

				# ignore channel_info.json and guild_info.json
				if filename.endswith('channel_info.json'):
					continue
				if filename.endswith('guild_info.json'):
					continue

				filename_without_base_directory = self.remove_base_directory(filename)
				files.append(filename_without_base_directory)


		return files

	def find_matrix_exports(self):
		print("finding Matrix bridge exports in " + self.base_directory)
		directory = self.base_directory
		files = []
		for filename in glob.glob(directory + '**/*.json', recursive=True, include_hidden=True):
			if self.looks_like_matrix_export(filename):
				files.append(self.remove_base_directory(filename))
		return files

	def looks_like_matrix_export(self, filename: str) -> bool:
		normalized = self.normalize_path(filename).lower()
		path_hint = any(part in normalized for part in ("/matrix", "/ooye", "/out-of-your-element"))
		try:
			with open(filename, "r", encoding="utf-8") as handle:
				prefix = handle.read(262144)
		except (UnicodeDecodeError, OSError):
			return False

		if not path_hint and not any(token in prefix for token in ('"room_id"', '"event_id"', '"origin_server_ts"', '"m.room.message"', '"m.sticker"')):
			return False

		try:
			payload = json.loads(prefix)
		except json.JSONDecodeError:
			# Large Matrix exports are still useful to detect without loading the
			# full file. DCE exports do not contain Matrix room/event markers.
			return '"room_id"' in prefix and ('"event_id"' in prefix or '"origin_server_ts"' in prefix)

		if not isinstance(payload, dict):
			return False
		if "guild" in payload and "channel" in payload and "messages" in payload:
			return False
		if "room_id" in payload and ("event_id" in payload or "events" in payload or "chunk" in payload):
			return True
		if "events" in payload and isinstance(payload["events"], list):
			return any(isinstance(item, dict) and ("event_id" in item or "raw" in item) for item in payload["events"][:20])
		if "chunk" in payload and isinstance(payload["chunk"], list):
			return any(isinstance(item, dict) and "event_id" in item for item in payload["chunk"][:20])
		if "rooms" in payload and isinstance(payload["rooms"], dict):
			return True
		return False

	def find_local_assets(self):
		print("finding local assets in " + self.base_directory)
		input_directory = self.base_directory
		all_files = {}
		# file can be extensionless and without a dash
		# valid file names
		#  - `magic-1ED77.jpg`
		#  - `bird-thumbnail-43c70443ab5ddf0a.png`
		#  - `D8ADB`
		regex_pattern = re.compile(r'.+(\-|\/)(?:[a-fA-F0-9]{5}|[a-fA-F0-9]{16})(?:\..+)?$')
		for path in glob.glob(input_directory + '**/*', recursive=True, include_hidden=True):
			path = path.replace('\\', '/')
			if regex_pattern.match(path):
				filename = os.path.basename(path)
				all_files[filename] = path

		return all_files

	def remove_base_directory(self, path: str):
		"""
		remove base directory from the start of the path
		ignore if path doesn't start with base directory
		"""
		if path == None:
			return None

		path = self.normalize_path(path)
		if not path.startswith(self.base_directory):
			print("path doesn't start with base directory: " + path)
			return path

		return path[len(self.base_directory):]

	def add_base_directory(self, path: str):
		"""
		add base directory to the start of the path
		if path already starts with base directory, do nothing
		"""
		path = self.normalize_path(path)
		if path.startswith(self.base_directory):
			print("path already starts with base directory: " + path)
			return path

		return self.base_directory + path

	def normalize_path(self, path: str):
		"""
		replace all backslashes with /
		"""
		return path.replace("\\", "/")
