# Project 5: Mixtape Bug Hunt — Submission

> AI disclosure: I used Claude Code to help navigate and summarize the codebase
> during orientation (file-by-file summaries and call-chain tracing). All bug
> diagnosis, fixes, and root-cause analysis decisions are documented below.

---

## Codebase Map

Mixtape is a Flask + SQLAlchemy JSON API. It follows a strict **route → service →
model** layering: routes only parse input and format responses, all business
logic lives in `services/`, and `models.py` defines the persistence layer. There
are no HTML templates — every endpoint returns JSON.

### Main files and their responsibilities

**`app.py`** — Application factory (`create_app`). Instantiates the shared
`SQLAlchemy` object (`db`), configures the SQLite database (`mixtape.db` by
default), registers the four blueprints under URL prefixes (`/songs`,
`/playlists`, `/users`, `/feed`), and calls `db.create_all()`. The `db` object
defined here is imported by every model and service.

**`models.py`** — Defines 6 models and 3 association tables. All primary keys are
string UUIDs; all timestamps are timezone-aware UTC.
- `User` — has `listening_streak` and `last_listened_at` (streaks), and a
  self-referential many-to-many `friends` relationship via the `friendships`
  table.
- `Song` — shared songs; `tags` is a many-to-many via `song_tags` (`lazy="subquery"`).
- `ListeningEvent` — one row per listen (`user_id`, `song_id`, `listened_at`);
  drives both streaks and the feed.
- `Rating` — score 1–5, one per (user, song) via a unique constraint.
- `Playlist` — songs joined via the `playlist_entries` association table, which
  carries an explicit **`position`** column (plus `added_by`, `added_at`). Order
  is explicit, not insertion order.
- `Notification` — `notification_type`, `body`, `read` flag; belongs to a user.

**`routes/`** — Thin Flask blueprints. Each handler reads request args/JSON,
delegates to exactly one service function, and wraps the result in `jsonify`.
`ValueError` from a service is caught and turned into a 400/404.
- `songs.py` → `/search`, `/<id>`, `/<id>/rate` (POST), `/<id>/listen` (POST)
- `playlists.py` → create, `/<id>`, `/<id>/songs` (GET + POST)
- `users.py` → `/<id>`, `/<id>/streak`, `/<id>/notifications`, mark-read
- `feed.py` → `/<id>/listening-now`, `/<id>/activity`

**`services/`** — Business logic (where all five bugs live):
- `streak_service.py` — records listening events and updates the consecutive-day
  streak (`update_listening_streak`).
- `feed_service.py` — `get_friends_listening_now` (recency-filtered, one row per
  friend) and `get_activity_feed` (most-recent N, unfiltered).
- `search_service.py` — `search_songs` (title/artist ilike match, joins tags) and
  `get_song`.
- `notification_service.py` — `create_notification` helper, `add_to_playlist`,
  `rate_song`, and notification retrieval/mark-read.
- `playlist_service.py` — `create_playlist`, `get_playlist_songs`
  (position-ordered), and playlist listing.

**`seed_data.py`** — Populates the DB with test data.
**`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`.

### Data flow trace — "a user adds a friend's song to a playlist"

1. Client sends `POST /playlists/<playlist_id>/songs` with JSON `{song_id, added_by}`.
2. `routes/playlists.py::add_song` parses the body, validates both fields are
   present, and calls `notification_service.add_to_playlist(playlist_id, song_id, added_by)`.
3. `add_to_playlist` loads the `Song`, the adding `User`, and the `Playlist`
   (raising `ValueError` → 400 if any is missing). If the song isn't already in
   `playlist.songs`, it appends it and commits.
4. If the adder is **not** the song's original sharer (`song.shared_by`), it calls
   `create_notification(user_id=song.shared_by, type="song_added_to_playlist", body=...)`.
5. `create_notification` writes a `Notification` row and commits.
6. The route returns `201 {"message": "Song added to playlist"}`. The sharer can
   later see the notification via `GET /users/<id>/notifications`.

### Data flow trace — "a user listens to a song" (streak path)

`POST /songs/<song_id>/listen` → `routes/songs.py::listen` →
`streak_service.record_listening_event(user_id, song_id)` → creates a
`ListeningEvent`, then calls `update_listening_streak(user, now)` which compares
`now.date()` to `user.last_listened_at.date()` and increments / resets /
no-ops accordingly → commits.

### Patterns I noticed

- **Uniform layering:** every route delegates immediately to one service function
  and does no business logic itself. To debug an endpoint, go straight to its
  service.
- **Shared `db` singleton:** `app.py` owns `db`; models and services import it.
  Services own their own `db.session.commit()` calls.
- **Explicit ordering:** playlists use a `position` column rather than relying on
  insertion order — so ordering/slicing logic matters when reading them back.
- **Errors as `ValueError`:** services signal "not found" / bad input by raising
  `ValueError`, which routes uniformly translate to HTTP errors.
- **Time is UTC everywhere**, but some comparisons mix calendar-day logic with
  fixed time windows — worth watching in the streak and feed services.

---

## The Five Issues (read, not yet fixed)

| # | Symptom | Service | Reproduced? |
|---|---------|---------|-------------|
| 1 | Listening streak keeps resetting | `streak_service.py` | ✅ yes |
| 2 | "Friends Listening Now" shows people from yesterday | `feed_service.py` | ✅ yes |
| 3 | Same song shows up twice in search | `search_service.py` | ❌ could not reproduce |
| 4 | No notification when a friend rates my song | `notification_service.py` | ✅ yes |
| 5 | Last song in a playlist never shows up | `playlist_service.py` | ✅ yes |

**Chosen for fixing: #1, #4, #5** (all reproduce cleanly and span three services;
#1 and #5 already have failing tests in the suite). **#2** is a stretch candidate.

---

## Milestone 2: Reproduction

Environment: `source .venv/bin/activate`, in-memory SQLite unless noted.

### Issue #1 — streak resets on Sunday ✅
The existing suite already reproduces it:
```
$ python -m pytest tests/test_streaks.py::test_streak_increments_on_sunday
FAILED — assert 1 == 2
```
**How to reproduce:** Call `update_listening_streak(user, saturday)` then
`update_listening_streak(user, sunday)` where Saturday→Sunday are consecutive
days (`2024-06-15` → `2024-06-16`). Expected streak `2`, actual `1`. The reset
only fires when *today* is Sunday (`weekday() == 6`) — any other consecutive-day
pair increments correctly. Data condition: a user with `last_listened_at` set to
the Saturday before a Sunday listen.

### Issue #5 — last song in a playlist is dropped ✅
Existing suite reproduces it:
```
$ python -m pytest tests/test_playlists.py::test_playlist_returns_all_songs
FAILED — len == 4, expected 5   (Track 5 missing)
```
**How to reproduce:** Seed a playlist with N songs (positions 1..N) and call
`get_playlist_songs(playlist_id)`. It returns N-1 songs, always missing the
highest-position (last) one. Deterministic — any non-empty playlist triggers it.

### Issue #4 — no notification when a friend rates my song ✅
No existing test; reproduced with a script:
```
sharer shares a song → friend calls rate_song(friend, song, 5)
→ get_notifications(sharer) returns 0   (expected 1)
```
**How to reproduce:** User A shares a song; user B (≠ A) rates it via
`rate_song`. Check A's notifications — none are created. Contrast with
`add_to_playlist`, which *does* call `create_notification` for the sharer. Data
condition: rater and sharer must be different users (a self-rating would not
notify anyway).

### Issue #2 — "listening now" shows friends from yesterday ✅ (stretch)
```
friend listens 20h ago (a prior calendar day) → get_friends_listening_now(user)
returns that friend   (should be empty — it wasn't 'now')
```
**How to reproduce:** Create a `ListeningEvent` for a friend timestamped ~20h ago
(so it falls on the previous calendar day but inside the 24h window) and call
`get_friends_listening_now`. The friend still appears, because the service uses a
rolling `timedelta(hours=24)` cutoff instead of a "today" boundary.

### Issue #3 — search duplicates ❌ could NOT reproduce
`search_songs("Crown")` on a song with 3 tags returns **1** row, not 3. The
existing `test_search_no_duplicates_multi_tag_song` **passes**. Root cause of the
non-repro: SQLAlchemy's ORM de-duplicates full-entity results by primary key, so
the `outerjoin(song_tags)` (which lacks `.distinct()`) still collapses to one
`Song` object per id. Per the milestone guidance ("if you can't reproduce a bug
after a genuine attempt, try a different one"), I dropped #3 in favor of #4.
> Note: the missing `.distinct()` is still a latent code smell — it would produce
> duplicates if the query ever selected columns instead of the mapped entity — but
> as written the reported behavior does not occur.

### Incidental finding (not one of the five)
`add_to_playlist` appends via the `Playlist.songs` relationship, which inserts
into `playlist_entries` **without** the required `position` / `added_by` columns,
raising `IntegrityError: NOT NULL constraint failed: playlist_entries.position`.
Out of scope for the five issues, but flagged here.

---

## Root-Cause Analysis

### Bug #1 — Listening streak resets on Sundays

- **Symptom:** A user's listening streak drops back to 1 even though they listened
  on consecutive days, but only when the current day is a Sunday.
- **How reproduced:** `update_listening_streak(user, saturday)` then
  `update_listening_streak(user, sunday)` for consecutive dates
  (`2024-06-15` → `2024-06-16`). Expected streak `2`, got `1`. Captured by the
  existing `tests/test_streaks.py::test_streak_increments_on_sunday`, which failed
  before the fix.
- **Root cause:** [streak_service.py:73](services/streak_service.py#L73) guarded
  the increment with `elif days_since_last == 1 and today.weekday() != 6:`.
  `weekday() == 6` is Sunday, so on Sundays the consecutive-day branch was skipped
  and control fell through to the `else`, which resets the streak to 1. The
  weekday check has nothing to do with streak logic — a streak should increment on
  any consecutive calendar day.
- **Fix:** Removed the `and today.weekday() != 6` clause so the branch is simply
  `elif days_since_last == 1:`.
- **Verification:** `pytest tests/test_streaks.py` — all 5 tests pass, including
  `test_streak_increments_on_sunday`.

### Bug #4 — No notification when a friend rates my song

- **Symptom:** When a friend adds your shared song to a playlist you get a
  notification, but when a friend *rates* your song you get nothing.
- **How reproduced:** User A shares a song; user B rates it via
  `rate_song(B, song, 5)`. `get_notifications(A)` returns 0 (expected 1). See
  `tests/test_notifications.py::test_rating_notifies_song_sharer` (added with this
  fix), which failed before the change.
- **Root cause:** The bug is architectural, not a typo. `add_to_playlist`
  ([notification_service.py:64](services/notification_service.py#L64)) calls
  `create_notification` for `song.shared_by` after committing. The parallel
  `rate_song` function persisted the rating but **never called
  `create_notification` at all** — the notification step was simply missing from
  that code path.
- **Fix:** After the rating commits, mirror the `add_to_playlist` pattern: if the
  rater is not the song's sharer, create a `song_rated` notification for
  `song.shared_by`. The `song.shared_by != user_id` guard prevents self-rating
  notifications, matching how `add_to_playlist` skips self-adds.
- **Verification:** New test `test_rating_notifies_song_sharer` passes; a manual
  check confirms a `song_rated` notification is created on a friend's rating and
  **not** created when the sharer rates their own song. Full suite green.

### Bug #5 — Last song in a playlist never shows up

- **Symptom:** Viewing a playlist's songs always omits the last (highest-position)
  song. A 5-song playlist shows 4.
- **How reproduced:** Seed a playlist with 5 songs at positions 1–5 and call
  `get_playlist_songs(playlist_id)`. It returns 4 songs, missing "Track 5".
  Captured by the existing `tests/test_playlists.py::test_playlist_returns_all_songs`
  and `test_playlist_returns_songs_in_order`, both of which failed before the fix.
- **Root cause:** [playlist_service.py:66](services/playlist_service.py#L66) built
  its result with `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice
  drops the final element of the position-ordered list. The query itself is
  correct (ordered ascending by position); the slice silently truncated it.
- **Fix:** Iterate over the full list: `[song.to_dict() for song in songs]`.
- **Verification:** `pytest tests/test_playlists.py` — all 3 tests pass, including
  the all-songs and ordering tests, and the empty-playlist case still returns `[]`.

### Bug #2 — "Friends Listening Now" shows people from yesterday (stretch)

- **Symptom:** The "Friends Listening Now" feed lists friends who last listened
  yesterday, not just those active today.
- **How reproduced:** Give a friend a `ListeningEvent` timestamped ~20h ago (a
  prior calendar day, but inside 24h) and call `get_friends_listening_now`. The
  friend still appears.
- **Root cause:** [feed_service.py](services/feed_service.py) used
  `RECENT_THRESHOLD = timedelta(hours=24)` with
  `cutoff = datetime.now(utc) - RECENT_THRESHOLD`. A rolling 24-hour window always
  reaches back into the previous calendar day, so someone who listened last night
  still counts as "now."
- **Fix:** Replaced the rolling window with a calendar-day boundary: the cutoff is
  now the start of the current day in UTC
  (`now.replace(hour=0, minute=0, second=0, microsecond=0)`). This matches the
  issue's "yesterday" framing and is consistent with the day-based logic already
  used for streaks. Removed the now-unused `RECENT_THRESHOLD` and `timedelta`
  import.
- **Design note:** "Listening now" is interpreted as "listened today (UTC)." An
  alternative would be a short rolling window (e.g. 15–30 min); I chose the
  calendar-day boundary because the reported bug is specifically about *yesterday*
  and the app already reasons in calendar days elsewhere.
- **Verification:** Manual check — a friend whose only listen was 5 minutes before
  midnight (yesterday) no longer appears; a friend who listened today does. Full
  suite still green (feed has no existing automated tests).
