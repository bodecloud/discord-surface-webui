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
- Matrix media message types with `mxc://` URLs retained in message metadata/content
- `m.sticker` events as message-like events
- OOYE event metadata when present

Next parity slices:

- Matrix media proxy for `mxc://` URLs so attachments render natively.
- Edit/redaction/reaction import into DCEF message history and reaction fields.
- Matrix room state import for names, topics, avatars, spaces, and membership.
- Live Matrix appservice ingestion instead of file/API payload import only.
- Bidirectional send/edit/delete/reaction actions matching OOYE's `d2m` and `m2d` action split.
