# WeFlow Raw Hook Primary Source Status

Date: 2026-07-01

## Position

The primary message source is now WeFlow local raw HTTP pull:

- `GET /api/v1/messages?talker=<id>&format=json&media=1&image=1&voice=1&video=1&emoji=1&file=1`
- OCR, window inspection, cache scanning, and SSE push are downgraded to diagnostics or triggers.
- `GET /api/v1/sessions/:id/messages` remains a compatibility/ChatLab endpoint, not the main ingestion path.
- The local project never uses WeChatFerry as the primary reader in this workflow.

The current implementation does not require the WeChat front-end chat to stay focused. WeChat must be running and WeFlow must have its local service active.

## Local Components

Project-side components:

- `WeFlowHttpBridge` pulls raw messages from WeFlow and writes normalized source events to `data/hook_events.jsonl`.
- `HookEventJsonlImporter` imports hook events into `data/backend_events.jsonl`.
- `BackendEventJsonlDriver` turns backend events into `RawWeChatMessage`, resolves attachments, and parses files/audio.
- `ConversationScheduler` processes different conversations concurrently while keeping each conversation queue serial.
- `ConversationLedgerStore` writes each conversation into its own `data/conversation_ledgers/<conversation_id>/messages.jsonl` and `conversation.md`.

Vendored WeFlow fork participating in this workflow:

- `vendor/reference/WeFlow-gitcode/electron/services/httpService.ts`
- `vendor/reference/WeFlow-gitcode/electron/services/exportService.ts`

Local WeFlow modifications made for this workflow:

- Raw `/api/v1/messages` can export `file` media in addition to image/voice/video/emoji.
- File cards reuse `exportService.exportFileAttachment()` through `exportApiFileAttachment()`.
- Raw API returns file metadata such as `fileName`, `fileSize`, `fileExt`, `fileMd5`, `xmlType`, `appMsgKind`, and `messageKey`.
- Exported media has `mediaType`, `mediaFileName`, `mediaUrl`, and `mediaLocalPath`.

## Concurrency Guarantees

Multiple concurrent sources means multiple WeChat conversations/talkers, not multiple guess-based data sources.

Raw pull isolation:

- Each worker owns one `talker` pull at a time.
- Different `talker` values can pull concurrently with `--workers N`.
- The same `talker` is protected by a per-talker lock file derived from the WeFlow state path.
- Global WeFlow state is merged under a separate state lock, so different talkers do not overwrite each other's cursor.

Ordering:

- WeFlow messages are sorted by `sortSeq`, then `createTime`, then `localId`, then `serverId`, then `messageKey`.
- Hook import stores `source_line_no`, `source_offset`, `batch_index`, and `import_sequence`.
- Backend metadata preserves `sort_key`, `create_time`, `local_id`, `server_id`, `message_key`, and `conversation_key`.
- Ledger sequence is per conversation file, not global across all conversations.

Write isolation:

- Hook source JSONL append is file locked.
- Hook importer now locks state while reading source offset, appending backend events, and writing the new offset.
- Backend JSONL append is file locked.
- Ledger writes are locked per `conversation_id`, so the same conversation is serial and different conversations can proceed in parallel.

## Captured Message/Media Types

Raw message metadata currently normalizes:

- Text
- Images
- Voice/audio, including audio path/name when WeFlow exports it
- Video
- Emoji
- File cards with local exported file path and file metadata
- Quotes when WeFlow exposes quote fields
- Self messages and user messages via `isSend`

Recall/delete support exists for WeFlow push recall events and ledger marking, but full recalled/deleted/history semantics are not the focus of this round.

## Local WeFlow Interfaces

Confirmed from local source:

- `GET /health` and `GET /api/v1/health`
- `GET /api/v1/messages`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/:id/messages`
- `GET /api/v1/contacts`
- `GET /api/v1/group-members`
- `GET /api/v1/media/*`
- `GET /api/v1/push/messages` for SSE trigger/replay
- SNS routes under `/api/v1/sns/*`

Security behavior confirmed from local source:

- Default host is `127.0.0.1`.
- Default port is `5031`.
- Non-health routes require `httpApiToken`.
- Token is accepted through `Authorization: Bearer`, `access_token` query, or body.
- Media serving checks path traversal under the `api-media` export directory.

Recommended for this workflow:

- Keep host on `127.0.0.1`.
- Use a strong `WEFLOW_API_TOKEN`.
- Do not expose WeFlow HTTP to LAN/WAN.
- Keep SNS routes unused for the current message-file workflow.

## WeChatFerry Status And Risk

WeChatFerry is vendored only as a reference at `vendor/reference/WeChatFerry-gitee`.

It is not used by the current primary pipeline.

Risk surface visible in local source/docs:

- RPC/HTTP client/server modules.
- Default RPC references include `0.0.0.0:10086` / `0.0.0.0:10087` in docs.
- HTTP/OpenAPI includes send text, image, file, XML, emotion, new friend, chatroom management, SQL query, and image decrypt routes.
- RPC protobuf includes send image/file and DB query functions.

For this round, do not run WeChatFerry unless explicitly needed later. If used later, bind it to localhost only and disable or firewall send/SQL routes.

## Commands

Health check:

```powershell
python -m app.personal_wechat_bot.main --data-dir data weflow-health --base-url http://127.0.0.1:5031 --token-env WEFLOW_API_TOKEN
```

History backfill for one or more talkers:

```powershell
python -m app.personal_wechat_bot.main --data-dir data pull-weflow-messages --base-url http://127.0.0.1:5031 --token-env WEFLOW_API_TOKEN --talker <wxid_or_roomid> --since 0 --context-only --message-limit 500 --max-pages 0 --workers 4 --extra-root "<WeFlow api-media path>" --verbose
```

Incremental polling:

```powershell
python -m app.personal_wechat_bot.main --data-dir data pull-weflow-messages --base-url http://127.0.0.1:5031 --token-env WEFLOW_API_TOKEN --talker <wxid_or_roomid> --forever --interval 1 --message-limit 100 --max-pages 1 --workers 4 --extra-root "<WeFlow api-media path>"
```

If no `--talker` is provided, the bridge calls `/api/v1/sessions` and pulls listed sessions up to `--session-limit`.

Use `--extra-root` for WeFlow exported media, usually a path ending in `api-media`; otherwise the project-side attachment safety policy may block parsing.

## Verification

Passed:

```powershell
python -m unittest tests.test_hook_source_bridge tests.test_hook_events tests.test_backend_events_cli tests.test_conversation_ledger
python -m unittest tests.test_backend_events tests.test_message_processor
python -m unittest discover -s tests
```

Full suite result: 311 tests OK, 1 skipped.

WeFlow TypeScript typecheck is not yet verified because local `node_modules` is missing in the vendored WeFlow tree. Previous `npx tsc` was not a valid check because it fetched an old `tsc` package.

