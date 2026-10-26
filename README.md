# AI News Portal — Bilingual News, RSS Diagnostics & 24-Hour Retention

IMPORTANT: Extract this ZIP into a NEW folder. Stop old backend/frontend terminals first. Do not keep running a previously extracted copy; Vite and Flask will continue serving whichever folder was launched.

This version includes:
- English and Hindi RSS sources across multiple categories.
- Automatic Groq article generation and publishing when `AUTO_PUBLISH=true`.
- Wikimedia Commons relevant-image search with category fallbacks and attribution.
- Automatic image backfill for already-published articles missing images.
- Image URL error fallback so broken image URLs do not show as broken icons.
- Public language filter (`हिन्दी`) and admin-only newsroom login at `/admin`.

## Setup
1. Extract to a fresh folder.
2. Edit `backend/.env`; set `GROQ_API_KEY=your_real_key` and change `ADMIN_PASSWORD`.
3. Run `START_BACKEND.bat` and keep it open.
4. Run `START_FRONTEND.bat` in another terminal.
5. Public site: `http://localhost:5173/`; admin: `http://localhost:5173/admin`.

## Troubleshooting
- Check `http://127.0.0.1:5000/api/health` to confirm the backend you started is this project.
- In the admin dashboard, check Automatic pipeline status and the last run message. If `GROQ_API_KEY` is a placeholder or invalid, generation will fail.
- Hindi filter only shows published articles whose source feed is marked `hi`; if the Hindi feeds fail, the dashboard run message records feed errors.
- Relevant image lookup requires internet access to Wikimedia Commons. If unavailable or no suitably licensed match is found, the UI shows a category placeholder rather than a broken image.
- The local scheduler only runs while the backend process remains running.
- RSS source availability and article image availability depend on the external publisher/services.
### RSS source coverage

The source list in `backend/sources.json` includes English and Hindi feeds across India, world, business, technology, sports, entertainment, science, health, education and lifestyle. Feed availability can change; check the backend's latest pipeline run/logs for sources that fail. RSS feeds provide headlines/excerpts and remain subject to each publisher's terms. Attribute sources and link to the original report; do not republish full articles without permission.


## 24-hour automatic deletion

- Every article is deleted 24 hours after its `published` timestamp, along with its related `news` row.
- The public article list/detail endpoints and admin article/news endpoints hide expired records even if the backend has not yet completed its next cleanup run.
- Cleanup runs at backend startup and during each automatic pipeline run (normally every 5 minutes). If the backend is stopped, physical deletion happens when it runs again; expired stories remain hidden from the API in the meantime.
- If an article image is a local file under the app-managed `uploads/` or `media/` folders, cleanup removes that file. Remote Wikimedia or other external image URLs are not deleted because they are not stored on this server.
- To prevent old RSS items from being imported again after their article is deleted, the database keeps a **SHA-256 fingerprint of the canonical source URL** in `expired_news_fingerprints`. It does not keep the article text/title/body, but this small deduplication metadata remains in the database.
- The 24-hour timer starts when the article is published. Unpublishing does not reset the timer; republishing sets a new publication timestamp.
- This deletes records from the active SQLite database, not copies you may have made elsewhere. If you create database backups or copies, apply your own backup-retention policy too.

## Existing database

The new `expired_news_fingerprints` table is created automatically on startup. Keep your existing `backend/news.db` when updating the project if you want to preserve current stories; take a copy before changing project versions. Old articles that were unpublished by an earlier version may have had their `published` timestamp cleared, so the system cannot infer their original publication time retroactively.


## Category publishing fix (Politics / Sports and other sections)

- The pipeline now selects pending stories fairly across categories instead of processing only the oldest 30 rows. This prevents an old backlog or repeated AI failures from starving newer Politics, Sports, and other categories.
- If the same story appears first in a broad India/World feed and later in a dedicated category feed, the more specific category is now applied.
- After replacing the project files, stop both old terminals, extract the updated ZIP into a fresh folder, start the backend and frontend from that folder, then use **Collect news now** in the admin dashboard. Keep the backend running.
- Existing published stories keep their current category unless their source URL is collected again from a more specific feed. Newly collected items use the corrected logic.
