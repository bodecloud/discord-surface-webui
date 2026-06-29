# build using (linux):
# docker build -t dcef .
# ----------------------

# build sveltekit static app
FROM docker.io/library/node:22.6.0-alpine3.19 as build
RUN mkdir -p /app/dcef/frontend
WORKDIR /app/dcef/frontend
COPY src/dcef/frontend/package.json src/dcef/frontend/package-lock.json ./
RUN npm install
COPY src/dcef/frontend/ .
RUN npm run build

# main image
FROM docker.io/library/mongo:6.0.5-jammy
WORKDIR /dcef
RUN apt-get update && apt-get install python3.11 python3-pip nginx wget -y
ARG TARGETARCH
ENV DISCORD_CHAT_EXPORTER_VERSION=2.47.3
RUN mkdir -p /dcef/tools /dcef/live-exports \
	&& python3.11 - <<'PY'
import io
import os
import stat
import urllib.request
import zipfile

version = os.environ["DISCORD_CHAT_EXPORTER_VERSION"]
arch = os.environ.get("TARGETARCH") or "amd64"
asset_arch = {"amd64": "x64", "arm64": "arm64", "arm": "arm"}.get(arch)
if asset_arch is None:
	raise SystemExit(f"unsupported TARGETARCH for DiscordChatExporter: {arch}")
url = f"https://github.com/Tyrrrz/DiscordChatExporter/releases/download/{version}/DiscordChatExporter.Cli.linux-{asset_arch}.zip"
with urllib.request.urlopen(url, timeout=120) as response:
	data = response.read()
with zipfile.ZipFile(io.BytesIO(data)) as archive:
	archive.extractall("/dcef/tools")
exe = "/dcef/tools/DiscordChatExporter.Cli"
os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
PY
RUN mkdir -p /dcef/exports/
COPY release/exports/ /dcef/exports/
COPY src/dcef/backend/preprocess/requirements.txt /dcef/backend/preprocess/requirements.txt
COPY src/dcef/backend/fastapi/requirements.txt /dcef/backend/fastapi/requirements.txt
COPY src/dcef/backend/configurator/requirements.txt /dcef/backend/configurator/requirements.txt
RUN python3.11 -m pip install -r /dcef/backend/preprocess/requirements.txt
RUN python3.11 -m pip install -r /dcef/backend/fastapi/requirements.txt
RUN python3.11 -m pip install -r /dcef/backend/configurator/requirements.txt
RUN mkdir -p /dcef/backend/nginx/logs/
COPY src/dcef/backend/nginx/conf/mime.types /dcef/backend/nginx/conf/mime.types
COPY src/dcef/backend/nginx/conf/nginx-docker.conf /dcef/backend/nginx/conf/nginx-docker.conf
COPY --from=build /app/_temp/frontend/ ./frontend/
COPY src/dcef/backend/preprocess/ ./backend/preprocess/
COPY src/dcef/backend/fastapi/ ./backend/fastapi/
COPY src/dcef/backend/configurator/main.py ./configurator.py
COPY src/dcef/backend/docker/run_container.sh ./run_container.sh
RUN chmod 777 /dcef/run_container.sh
EXPOSE 21011
CMD /dcef/run_container.sh
