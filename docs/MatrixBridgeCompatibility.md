# Matrix Bridge Compatibility

This fork treats Matrix and Discord as two protocol views over one archive UI.

The first compatibility layer is intentionally at the data boundary:

- DiscordChatExporter JSON continues through the existing DCEF preprocessor.
- Matrix Client-Server room exports and Out Of Your Element `/api/message`-style payloads are detected by `FileFinder.find_matrix_exports`.
- `MatrixProcessor` normalizes Matrix rooms into the existing DCEF Mongo shape:
  - one synthetic guild: `Matrix Bridge`
  - each Matrix room becomes a DCEF channel
  - each Matrix sender becomes a DCEF author
  - each Matrix message/sticker event becomes a DCEF message
  - Matrix-native IDs and raw events are preserved under `bridge`
- The backend exposes an OOYE-compatible lookup:
  - `GET /api/message?message_id=<numeric id>`
  - `GET /api/bridge/message?message_id=<numeric id>`

Current event coverage:

- `m.room.message` text events
- Matrix media message types with HTTP media URLs as DCEF attachments
- Matrix media message types with `mxc://` URLs rewritten to `/api/bridge/mxc/...` attachment URLs
- `m.sticker` events as message-like events
- `m.replace` edit events into DCEF message history and `timestampEdited`
- `m.annotation` reaction events into DCEF reactions
- `m.room.redaction` events into DCEF deleted-message state
- OOYE event metadata when present
- Related Matrix events in the OOYE-compatible `events` response array
- Matrix media redirect endpoint:
  - `GET /api/bridge/mxc/{server}/{media_id}`
- Matrix room state:
  - `m.room.name` into channel names
  - `m.room.topic` into channel topics
  - `m.room.avatar` into preserved bridge metadata
  - `m.room.member` display names and avatars into DCEF authors
  - `m.space.child` / `m.space.parent` into Matrix space-backed categories
- Encrypted Matrix media payloads that expose `content.file.url`
- Matrix appservice live ingestion:
  - `PUT /api/_matrix/app/v1/transactions/{txnId}`
  - legacy `PUT /api/transactions/{txnId}`
  - optional `MATRIX_APPSERVICE_HS_TOKEN` / `MATRIX_HS_TOKEN` Bearer-token verification
  - idempotent transaction processing by transaction ID
- Matrix appservice registration and namespace helpers:
  - `GET /api/bridge/appservice/registration`
  - `GET /api/bridge/appservice/registration?format=json`
  - user namespace queries at `/_matrix/app/v1/users/{userId}` and `/users/{userId}`
  - room alias namespace queries at `/_matrix/app/v1/rooms/{roomAlias}` and `/rooms/{roomAlias}`
- Bidirectional action outbox matching OOYE's `m2d` / `d2m` split:
  - `POST /api/bridge/actions/m2d/{send|edit|delete|reaction|remove_reaction}`
  - `POST /api/bridge/actions/d2m/{send|edit|delete|reaction|remove_reaction}`
  - `POST /api/bridge/actions/batch`
  - `POST /api/bridge/actions/refresh`
  - actions read credentials from request payload, Mongo `config`, or env vars
  - when credentials are absent, actions are stored as pending outbox entries instead of failing

Next parity slices:

- Rich OOYE converters for replies, polls, embeds, files, webhooks, PluralKit, Matrix puppets, and power-level-sensitive moderation.
