# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

I used Claude Code (AI assistant) throughout this project. Being specific about
*how*, including where it helped and where it was wrong or incomplete:

**Codebase navigation (Milestone 1).** I had the AI summarize each service and
route file ("what is this module responsible for, what does each function do") and
trace the `route → service → model` call chains for the codebase map. This was the
most reliable use — summarizing code that already exists. I still read every file
myself and confirmed each trace against the source before writing the map.

**Debugging / investigation (Milestone 3).** I used AI for the *understanding*
step, not the *diagnosis* step:
- Confirmed a factual detail I'd already narrowed to: Python's `datetime.weekday()`
  returns `6` for Sunday (this is what makes Bug #1's `weekday() != 6` guard fire
  on Sundays). I verified it in a REPL rather than taking the answer on faith.
- Asked it to compare the *structural* difference between the `rate_song` and
  `add_to_playlist` code paths for Bug #4. It correctly pointed out `rate_song`
  has no `create_notification` call — but I confirmed that by reading both
  functions end-to-end myself, because "these look structurally different" is only
  a lead, not proof.

**Where AI was wrong / incomplete — and how I caught it.** Reading the code alone
(and an initial AI read of it) flagged Bug #3 as a real duplicate-rows bug: the
search query does `outerjoin(song_tags)` with no `.distinct()`, which *looks* like
it should return one row per tag. That diagnosis was plausible and wrong. When I
actually ran `search_songs()` against a 3-tag song, it returned **1** row, not 3,
because SQLAlchemy's ORM de-duplicates full-entity results by primary key. This is
exactly the failure mode the assignment warns about — a plausible-from-reading
diagnosis that only running the code disproves. It's why I reproduce every bug
before touching it, and why I dropped #3 rather than "fixing" a bug that doesn't
occur. Running the code also surfaced an incidental `IntegrityError` in
`add_to_playlist` that neither reading nor the AI had predicted.

**Not used for:** deciding which bugs were real (I reproduced each one with a script
or failing test), or accepting any fix without verifying it against a test or manual
run.

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

## Summary of Work

**Bugs fixed (4 of 5):** #1 (streak), #4 (notifications), #5 (playlist) — required
three — plus stretch bug #2 (feed). Each is its own commit on `bugfix/mixtape`.
Bug #3 was investigated but does not reproduce (see Milestone 2); the reasoning is
documented rather than a fix applied.

**Stretch features completed:**
- Fixed a 4th bug (#2, feed).
- Wrote regression tests: `tests/test_notifications.py` (`test_rating_notifies_song_sharer`,
  `test_self_rating_does_not_notify`) — these fail against the pre-fix code and
  guard Issue #4 going forward.

**Test status:** `pytest tests/` → 15 passed (was 13 with 3 failing before fixes;
+2 new notification tests).

## Root-Cause Analysis

Each entry follows the required five fields: reproduction, how the root cause was
found (navigation path), the precise root cause, and the fix + side-effect check.

---

### Bug #1 — "My listening streak keeps resetting"

**How I reproduced it.** I ran the existing test suite first and saw
`tests/test_streaks.py::test_streak_increments_on_sunday` fail with `assert 1 == 2`.
To confirm outside the test, I called the service directly with controlled dates:
`update_listening_streak(user, 2024-06-15)` (Saturday) then `(user, 2024-06-16)`
(Sunday). The streak went 1 → 1 instead of 1 → 2. I then tried a non-weekend
consecutive pair (Mon→Tue) and it incremented correctly — that isolated the
trigger to "today is Sunday," matching the intermittent report.

**How I found the root cause.** Navigation path: `routes/songs.py::listen`
(`POST /songs/<id>/listen`) → `streak_service.record_listening_event` →
`streak_service.update_listening_streak`. I read `update_listening_streak`
top-down, writing down each branch. The `days_since_last == 0` (same day) and
`else` (reset) branches were fine. The moment of confidence was reading the middle
branch: `elif days_since_last == 1 and today.weekday() != 6:`. I confirmed via a
one-line REPL check that `datetime(2024, 6, 16).weekday() == 6`, i.e. Sunday, so
the extra condition specifically fails on Sundays.

**The root cause.** In [streak_service.py:73](services/streak_service.py#L73) the
consecutive-day branch was `elif days_since_last == 1 and today.weekday() != 6:`.
Python's `datetime.weekday()` returns 6 for Sunday. So when a user listened
yesterday and the current day is Sunday, `days_since_last == 1` is true but
`today.weekday() != 6` is **false**, so the `elif` is skipped and execution falls
through to the `else`, which sets `user.listening_streak = 1`. The weekday check is
unrelated to streak logic — a streak should increment on *any* consecutive
calendar day — so it caused a correct streak to reset every Sunday.

**My fix and side-effect check.** Removed the `and today.weekday() != 6` clause,
leaving `elif days_since_last == 1:`. Side-effect check — I verified both sides of
the day boundary and the neighbouring branches still behave: new user → streak 1;
same-day second listen → no change; skipped day (`days_since_last >= 2`) → still
resets to 1; consecutive days on any weekday (incl. Sat→Sun) → increments. All 5
`test_streaks.py` cases pass. The change touches only that one condition, so
`record_listening_event`/`get_streak` are unaffected.

---

### Bug #4 — "I got notified when a friend added my song to a playlist but not when they rated it"

**How I reproduced it.** No existing test covered this, so I wrote a script: user A
shares a song, user B calls `rate_song(B, song, 5)`, then I read
`get_notifications(A)` — it returned 0 (expected 1). I contrasted it with the
playlist path in the same session and saw that path *does* produce a notification.
I turned this into `tests/test_notifications.py::test_rating_notifies_song_sharer`,
which failed against the pre-fix code.

**How I found the root cause.** The hint said this was architectural, so I compared
the two sibling functions in `notification_service.py` line-by-line. Navigation
path: `routes/songs.py::rate` (`POST /songs/<id>/rate`) → `notification_service.rate_song`,
versus `routes/playlists.py::add_song` → `notification_service.add_to_playlist`.
`add_to_playlist` ends with an `if song.shared_by != added_by_user_id:` block that
calls `create_notification`. Reading `rate_song` end-to-end, I confirmed it
validates the score, upserts the `Rating`, commits, and returns — with **no**
`create_notification` call anywhere. That structural absence (not a wrong argument
or typo) was the confirmation.

**The root cause.** `rate_song`
([notification_service.py:73](services/notification_service.py#L73)) never invoked
`create_notification`. The notification step that exists in the parallel
`add_to_playlist` path was simply missing from the rating path, so rating a song
persisted the `Rating` but produced no notification for the song's sharer.

**My fix and side-effect check.** After the rating commits, I mirrored the
`add_to_playlist` pattern: `if song.shared_by != user_id:` create a `song_rated`
notification addressed to `song.shared_by`. The guard prevents notifying a user
about their own rating (matching how `add_to_playlist` skips self-adds). Side-effect
check — I confirmed the rating upsert still works for both new and updated ratings
(the unique constraint path is unchanged), that a self-rating creates no
notification, and that `get_notifications` ordering/`add_to_playlist` behaviour are
untouched. Added two regression tests; full suite passes (13 → 15).

---

### Bug #5 — "The last song in a playlist never shows up"

**How I reproduced it.** The existing `tests/test_playlists.py::test_playlist_returns_all_songs`
failed (`len == 4`, expected 5) and `test_playlist_returns_songs_in_order` failed
showing Track 5 missing. I confirmed independently by calling
`get_playlist_songs(playlist_id)` on a 5-song seeded playlist — it returned 4,
always dropping the highest-position song.

**How I found the root cause.** Navigation path: `routes/playlists.py::get_songs`
(`GET /playlists/<id>/songs`) → `playlist_service.get_playlist_songs`. I read the
function and verified the SQL was correct: it joins `playlist_entries` and orders
`asc(playlist_entries.c.position)`. Since the query was sound, I looked at what
happened to its result and found the return statement sliced the list. That single
line was the cause, not the query.

**The root cause.** [playlist_service.py:66](services/playlist_service.py#L66)
returned `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice removes the
last element of the position-ordered list, so the final (highest-position) song is
silently dropped from every non-empty playlist. (The docstring even claims it
"returns all songs," so the slice contradicts the intended behaviour.)

**My fix and side-effect check.** Changed the slice to the full list:
`[song.to_dict() for song in songs]`. Side-effect check — both boundaries: the
first song (position 1) was never affected and is still present, and the last song
now appears; ordering is preserved (still `asc(position)`); and the empty-playlist
case still returns `[]` without error (`songs[:]` vs `songs` both handle empty
lists, but I ran `test_empty_playlist_returns_empty_list` to be sure). All 3
`test_playlists.py` tests pass.

---

### Bug #2 — "Friends Listening Now shows people from yesterday" (stretch)

**How I reproduced it.** No existing test. I created a `ListeningEvent` for a friend
timestamped ~20h ago — a prior calendar day, but still inside 24 hours — and called
`get_friends_listening_now(user)`. The friend still appeared in the feed. I also
verified a same-day event still appears, so the feed wasn't simply broken; it was
including too much.

**How I found the root cause.** Navigation path: `routes/feed.py::listening_now`
(`GET /feed/<id>/listening-now`) → `feed_service.get_friends_listening_now`. I read
the function and focused on the cutoff computation, since the symptom is a time
boundary. I found the module-level `RECENT_THRESHOLD = timedelta(hours=24)` and
`cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`, with the query filtering
`listened_at >= cutoff`. Confirming the arithmetic made it obvious: at any time of
day, `now - 24h` lands in the previous calendar day, so "yesterday evening" events
pass the filter.

**The root cause.** [feed_service.py](services/feed_service.py) defined recency as a
rolling 24-hour window. A rolling window is not the same as "today": e.g. at 14:00
it admits everything back to 14:00 the previous day. So a friend who listened last
night satisfies `listened_at >= now - 24h` and is shown as "listening now," which
is the reported "people from yesterday" behaviour.

**My fix and side-effect check.** Replaced the rolling window with a calendar-day
boundary — `cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)` (start
of the current UTC day) — and removed the now-unused `RECENT_THRESHOLD` constant and
`timedelta` import. I chose a calendar-day boundary (rather than a shorter rolling
window) because the reported bug is specifically about *yesterday*, and the app
already reasons in UTC calendar days for streaks — so this is consistent. Side-effect
check — both sides of the boundary: an event 5 minutes before midnight (yesterday)
is now excluded, an event just after midnight (today) is included; the per-friend
dedup and ordering are unchanged; and `get_activity_feed` (which is intentionally
*not* recency-filtered) was deliberately left untouched. Full suite still green.

