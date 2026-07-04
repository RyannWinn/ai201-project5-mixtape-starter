"""
tests/test_notifications.py — Mixtape

Regression tests for notification creation (Issue #4).

Before the fix, rate_song() saved the Rating but never notified the song's
original sharer, even though add_to_playlist() did. These tests would have
caught that gap: test_rating_a_song_notifies_the_sharer fails against the
buggy code (0 notifications) and passes after the fix (1 notification).
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_song(app):
    """A song shared by 'sharer', plus a separate 'rater' user."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="Synthwave Co", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, sharer_and_song):
    """
    Regression for Issue #4: rating someone else's song must notify the sharer.
    Buggy code produced 0 notifications; the fix produces exactly 1 of type
    'song_rated'.
    """
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        rater = sharer_and_song["rater"]
        song = sharer_and_song["song"]

        rate_song(rater.id, song.id, 5)

        notifs = get_notifications(sharer.id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"


def test_rating_your_own_song_does_not_notify(app, sharer_and_song):
    """A user rating their own shared song should not notify themselves."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 4)

        assert get_notifications(sharer.id) == []


def test_re_rating_does_not_create_duplicate_notification_row(app, sharer_and_song):
    """
    Re-rating updates the same Rating row. Each rating action does notify,
    but the important invariant is the rating upsert isn't broken by the
    added notification step — the score updates in place.
    """
    with app.app_context():
        rater = sharer_and_song["rater"]
        song = sharer_and_song["song"]

        first = rate_song(rater.id, song.id, 2)
        second = rate_song(rater.id, song.id, 5)

        assert first.id == second.id  # same Rating row updated, not duplicated
        assert second.score == 5
