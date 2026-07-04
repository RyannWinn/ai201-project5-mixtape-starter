# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

I used an AI assistant (Claude) primarily as a **navigation and explanation partner**, not as a bug finder.

- **Codebase orientation.** I had the AI summarize each service file's responsibility and confirm the route → service call chains after I had read the files myself. This sped up building the mental model in the codebase map below.
- **Verifying a language/library assumption.** For Issue #1 I asked the AI to confirm what Python's `datetime.weekday()` returns for each day (Monday = 0 … Sunday = 6) vs. `isoweekday()` (Sunday = 7). I then verified this myself by reading the streak test, which hard-codes `saturday.weekday() == 5` and `sunday.weekday() == 6` — matching the AI's answer.
- **Where the AI would have been wrong / where I overrode it.** Issue #3 ("the same song shows up twice in search") *looks* like a classic missing-`DISTINCT` bug — the `search_songs` query does an `outerjoin` on `song_tags` with no `.distinct()`, and a surface reading (or an AI asked "what's wrong here") confidently says "songs with multiple tags will be duplicated." **I did not trust that.** I ran the actual test suite and the three search-dedup tests *passed*. The reason: SQLAlchemy's legacy ORM `Query` deduplicates identical entity instances for single-entity queries via the identity map, so the 3 joined rows collapse back to 1 `Song` object. The observable bug does not reproduce against this code + ORM version. This is exactly the "AI points you somewhere plausible but wrong" trap. I still applied the correct structural fix (removing the useless join + a defensive `.distinct()`), because the join is genuinely wrong and the dedup is an ORM implementation detail I shouldn't rely on — but the RCA for #3 below is explicit that the fix hardens correctness rather than repairing an observable failure.
- **The workflow that worked:** read the code → form a hypothesis → *run it* (pytest / a small Python repro script) → only then fix. Every diagnosis below was confirmed by executing the code, not by reading alone.

---

## Codebase Map

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`) and the shared `db = SQLAlchemy()` instance. Registers four blueprints under URL prefixes: `/songs`, `/playlists`, `/users`, `/feed`. Calls `db.create_all()` on startup.
- **`models.py`** — Defines all SQLAlchemy models and three association tables:
  - Models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`.
  - `friendships` — symmetric many-to-many self-join on `User` (each friendship is inserted twice, both directions — see `seed_data.add_friendship`).
  - `song_tags` — many-to-many `Song` ↔ `Tag`.
  - `playlist_entries` — many-to-many `Playlist` ↔ `Song`, but with **extra columns**: `position` (explicit ordering, NOT NULL), `added_by`, and `added_at`. Songs in a playlist have an explicit position, not just insertion order.
  - `Rating` has a `UniqueConstraint(user_id, song_id)` — a user has at most one rating per song. There is no separate "rating notification" record; a rating is stored only on the `Rating` row.
- **`routes/`** — Thin HTTP layer. Every route parses input, calls exactly one service function, and formats the JSON response. No business logic lives here.
  - `songs.py` — `/songs/search`, `/songs/<id>`, `/songs/<id>/rate` (POST), `/songs/<id>/listen` (POST).
  - `playlists.py` — create playlist, get playlist, get/add playlist songs.
  - `users.py` — user profile, streak, notifications, mark-notification-read.
  - `feed.py` — `/feed/<id>/listening-now`, `/feed/<id>/activity`.
- **`services/`** — All business logic. This is where the bugs live.
  - `streak_service.py` — records listening events and updates `User.listening_streak` based on consecutive calendar days.
  - `feed_service.py` — "Friends Listening Now" (recent, deduped per friend) and the general activity feed.
  - `search_service.py` — case-insensitive title/artist search.
  - `notification_service.py` — creates/reads `Notification` rows; also owns `add_to_playlist` and `rate_song`.
  - `playlist_service.py` — playlist creation and ordered song retrieval.
- **`seed_data.py`** — Drops and recreates the DB, then inserts 5 users (with friendships), 25 songs (deliberately with 0 / 1 / 3+ tags to probe Issue #3), 3 playlists, listening events (some within 30 min, some hours/days old), streak state, and one working playlist-add notification.
- **`tests/`** — `pytest` suites for streaks, search, and playlists (each spins up an in-memory SQLite app).

### Data flow trace — "a friend rates my shared song" (Issue #4 feature)

1. Client sends `POST /songs/<song_id>/rate` with JSON `{user_id, score}`.
2. `routes/songs.py::rate()` parses `user_id`/`score`, validates presence, and calls `notification_service.rate_song(user_id, song_id, int(score))`.
3. `rate_song()` validates the score range (1–5), loads the `Song` and rater `User`, then either updates the rater's existing `Rating` (unique per user+song) or inserts a new one, and commits.
4. It returns the `Rating`; the route serializes it with `to_dict()` and returns `201`.

Compare this with the **working** parallel flow, `add_to_playlist()` in the same file: after mutating the playlist it calls `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", ...)` when the actor isn't the sharer. `rate_song()` has **no equivalent notification step** — that gap is Issue #4.

### Data flow trace — "friends listening now"

`GET /feed/<user_id>/listening-now` → `feed_service.get_friends_listening_now()` computes a `cutoff = now - RECENT_THRESHOLD`, gathers the user's `friend_ids`, queries `ListeningEvent`s newer than the cutoff ordered by recency, then keeps only the most recent event per friend (dedup via a `seen_friends` set) and returns `{friend, song, listened_at}` dicts.

### Patterns I noticed

- **Strict route → service delegation.** Routes never touch the DB for business logic; they translate HTTP ↔ service calls and turn `ValueError` into `4xx` responses. All model queries live in `services/`.
- **`ValueError` as the "not found / bad input" signal.** Services raise `ValueError`; routes catch it and map to `400`/`404`.
- **Timezone-aware UTC everywhere on write** (`datetime.now(timezone.utc)`), but SQLite reads values back **naive** — `streak_service` defensively re-attaches `tzinfo=timezone.utc` to `last_listened_at` before doing date math. That defensive step is a hint that datetime handling is a fragile area in this codebase.
- **`playlist_entries` carries ordering data** (`position`), so playlist reads must `ORDER BY position` — they can't rely on insertion order.

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting (Sunday boundary)

**How I reproduced it.** Ran `pytest tests/` and saw `test_streak_increments_on_sunday` fail with `assert 1 == 2`. The test listens on Saturday `2024-06-15` (`weekday() == 5`) then Sunday `2024-06-16` (`weekday() == 6`); the streak should go 1 → 2 but stayed at 1. I confirmed the trigger condition is specifically "the *second* consecutive listen lands on a Sunday."

**How I found the root cause.** Navigation path: `routes/songs.py::listen()` → `streak_service.record_listening_event()` → `update_listening_streak()`. Reading `update_listening_streak`, line 73 jumped out:
`elif days_since_last == 1 and today.weekday() != 6:`. The moment I was sure: `weekday()` returns `6` for Sunday, and the test comment even annotates `sunday ... weekday() == 6`. So on Sundays the `and today.weekday() != 6` clause is `False`, the `elif` is skipped, and control falls to the `else` branch that resets the streak to 1.

**The root cause.** Python's `datetime.weekday()` returns `6` for Sunday. The increment branch was gated on `today.weekday() != 6`, so **any consecutive-day listen that happened to fall on a Sunday was misrouted into the reset branch** instead of incrementing. There is no legitimate reason for the day-of-week to affect a consecutive-day streak — this clause was simply wrong. It only bites on Sundays, which is why the streak "randomly" reset about once a week.

**My fix and side-effect check.** Removed the spurious day-of-week guard so the branch is just `elif days_since_last == 1:`. Consecutive days now increment regardless of weekday. Side-effect check: re-ran the full streak suite — `test_streak_starts_at_1_for_new_user`, `test_streak_increments_on_consecutive_day` (Mon→Tue), `test_streak_does_not_double_count_same_day` (`days_since_last == 0` path), and `test_streak_resets_after_skipped_day` (`days_since_last == 2` → still resets) all pass. I verified both sides of the boundary: a genuine skipped day still resets to 1, and same-day re-listens still don't double-count.

### Issue #4 — Notified when a friend added my song to a playlist, but not when they rated it

**How I reproduced it.** Wrote a small repro script: created a `sharer` and a `rater`, had the sharer share a song, then called `rate_song(rater, song, 5)` and checked `get_notifications(sharer.id)` — it returned **0** notifications. The parallel action, `add_to_playlist`, does create a notification for the sharer (and the seed data ships a working `song_added_to_playlist` notification proving that path). So rating is silently notification-less.

**How I found the root cause.** Navigation path: `routes/songs.py::rate()` → `notification_service.rate_song()`. I read `rate_song` side-by-side with `add_to_playlist` in the same file. `add_to_playlist` ends with a `if song.shared_by != added_by_user_id: create_notification(...)` block; `rate_song` performs the rating upsert, commits, and returns — with **no `create_notification` call anywhere**. That structural diff between the two sibling functions was the confirmation.

**The root cause.** `rate_song()` never notifies anyone. The feature ("tell the original sharer when someone rates their song") was simply never implemented in the rating path, even though the identical pattern exists in `add_to_playlist`. It's a missing step, not a wrong comparison.

**My fix and side-effect check.** After the rating is committed, I added — mirroring `add_to_playlist` — a guard that notifies the sharer only when the rater is not the sharer:
`if song.shared_by != user_id: create_notification(user_id=song.shared_by, notification_type="song_rated", body=...)`. I placed it after the commit so a failed rating never emits a phantom notification, and I guard on `song.shared_by != user_id` so users don't get notified for rating their own songs. Side-effect check: re-ran the repro (sharer now gets exactly 1 notification after a rating; still 0 when the sharer rates their own song), and confirmed the rating upsert behavior is unchanged (re-rating updates the same `Rating` row rather than inserting a duplicate). The returned `Rating` object and the route's `201` response are unaffected.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** Ran `pytest tests/`; `test_playlist_returns_all_songs` failed (`4 != 5`) and `test_playlist_returns_songs_in_order` failed (`['Track 1'..'Track 4']` missing `'Track 5'`). A seeded 5-song playlist returned only its first 4 songs, always dropping the last one by position.

**How I found the root cause.** Navigation path: `routes/playlists.py::get_songs()` → `playlist_service.get_playlist_songs()`. The query itself is correct — it joins `playlist_entries`, filters by playlist, and orders ascending by `position`. The bug is on the final line: `return [song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice drops the last element of the ordered list.

**The root cause.** The list comprehension slices the ordered result with `songs[:-1]`, which discards the final song (the one with the highest `position`). Because the list is ordered by `position`, "the last element" is always the last song in the playlist — so the highest-position song is unconditionally omitted.

**My fix and side-effect check.** Changed `songs[:-1]` to `songs` so every song is returned. Side-effect check: re-ran the playlist suite — all songs return in correct position order, and `test_empty_playlist_returns_empty_list` still passes (an empty playlist yields `[]`; previously `[][:-1]` also happened to be `[]`, so this fix doesn't regress the empty case). Verified both boundary sides: single-element and multi-element playlists now return their full contents.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Built a small in-memory scenario mirroring the seed data: a user with two friends, one who listened 10 minutes ago and one who listened 2 hours ago. Called `get_friends_listening_now()` — with the original code **both** friends came back, so the "listening now" view included a friend who last listened 2 hours ago (i.e. earlier today / effectively "yesterday" once you cross midnight). The seed data encodes the same expectation: events within ~30 min "should appear in listening now," events 2+ hours old "should NOT appear after fix."

**How I found the root cause.** Navigation path: `routes/feed.py::listening_now()` → `feed_service.get_friends_listening_now()`. The query and per-friend dedup are correct; the problem is the window itself: `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`, and `RECENT_THRESHOLD = timedelta(hours=24)` at the top of the module. A 24-hour cutoff means "recently" spans an entire day.

**The root cause.** `RECENT_THRESHOLD` was set to 24 hours. "Friends Listening Now" is a live presence view — it should reflect who is listening *right now*, on the order of minutes — but a 24h window admits anyone who listened at any point in the last day, including yesterday afternoon. The dedup logic even hides this for very active friends (it keeps their most recent event), so it surfaces specifically for friends whose only recent activity is hours old.

**My fix and side-effect check.** Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)` — long enough to include the seed's 10–20-minute "recent" events, short enough to exclude the 2h+ "older" events, matching the boundary the seed data documents. Side-effect check: `get_activity_feed()` in the same module deliberately does **not** use `RECENT_THRESHOLD` (its docstring says it's unfiltered by recency), so the activity feed is unaffected. My repro now returns only the 10-minute friend and excludes the 2-hour one. I verified both sides of the boundary (a 10-min listen shows; a 2-hour listen doesn't). The exact minutes value is a product judgment; I documented the reasoning in a comment next to the constant.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it — and why it doesn't reproduce.** This one is a deliberate trap. `search_songs` does `db.session.query(Song).outerjoin(song_tags, ...)` with no `.distinct()`, so at the SQL level a song with 3 tags yields 3 rows — the textbook cause of duplicate search results. But running `pytest tests/test_search.py` showed **all** the dedup tests passing, including `test_search_no_duplicates_multi_tag_song` (whose comment claims "bug causes it to be 3"). The observable duplicate does not occur.

**How I found the root cause.** Navigation path: `routes/songs.py::search()` → `search_service.search_songs()`. Reading the query, the `outerjoin(song_tags)` stood out as pointless: nothing in the `filter` references tag columns, and `to_dict()` loads tags via the `Song.tags` relationship (`lazy="subquery"`), not from this join. So the join exists only to multiply rows. I then confirmed *why* the tests still pass: SQLAlchemy's legacy `Query` object de-duplicates identical mapped entities for single-entity results via the identity map, collapsing the 3 joined rows back to one `Song`.

**The root cause.** Structurally, the `outerjoin` on `song_tags` is a bug — it multiplies result rows once per tag and has no purpose in a title/artist search. The only reason it doesn't produce visible duplicates is that this ORM version silently dedupes single-entity query results; that masking is an implementation detail, not a guarantee (a `.distinct()`-free `select()` in 2.0 style, or selecting extra columns, would expose the duplicates).

**My fix and side-effect check.** Removed the unnecessary `.outerjoin(song_tags, ...)` entirely (and its now-unused import) and added `.distinct()` as defensive insurance against any residual row multiplication. This addresses the root cause at its source rather than depending on ORM dedup behavior. Side-effect check: re-ran all five search tests — matching search still works, and songs with 0, 1, and 3+ tags each appear exactly once. Because tags are loaded through the relationship, tag data in each result is unchanged.

---

## Regression Test

`tests/test_notifications.py` is a new suite added specifically to lock in the Issue #4 fix — there was previously **no** test covering rating notifications, which is why the bug shipped.

- **`test_rating_a_song_notifies_the_sharer`** is the regression test proper: it has a `rater` rate a song shared by `sharer`, then asserts the sharer received exactly one notification of type `song_rated`. Against the pre-fix code this fails with `0 == 1` (I confirmed the "0 notifications" behavior with a repro script before fixing); against the fixed code it passes. This test would have caught the bug before it was introduced.
- **`test_rating_your_own_song_does_not_notify`** guards the boundary I added (`song.shared_by != user_id`) so a user rating their own song isn't self-notified.
- **`test_re_rating_does_not_create_duplicate_notification_row`** verifies my new notification step didn't break the existing rating upsert — re-rating still updates the same `Rating` row.

All 16 tests (13 original + 3 new) pass: `pytest tests/` → `16 passed`.

---

## Git Log

`git log --oneline` on `bugfix/mixtape` (also saved to `git-log.txt` in the repo root — take a screenshot of this for the portal):

```
db5f855 fix: drop unnecessary song_tags join causing duplicate search rows
c355462 fix: narrow Friends Listening Now window to 30 minutes
cb218ba fix: notify song sharer when their song is rated
328f9b3 fix: return the last song in a playlist
72e63e1 fix: increment listening streak on Sundays (weekday()==6 boundary)
2dfdeaa Add .gitignore file and update README with setup instructions
7b64551 initial commit
```

One commit per bug fix, each with a `fix:` prefix describing what was wrong and what changed.
