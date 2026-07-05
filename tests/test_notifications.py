"""
tests/test_notifications.py — Mixtape

Regression tests for notification logic (Issue #4).
These would have caught the missing "song rated" notification before it shipped.
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
    """A user who shares a song, plus a separate friend who can rate it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([sharer, friend])
        db.session.flush()

        song = Song(title="My Song", artist="Me", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "friend": friend, "song": song}


def test_rating_notifies_song_sharer(app, sharer_and_song):
    """Rating a song should notify the user who originally shared it."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        friend = sharer_and_song["friend"]
        song = sharer_and_song["song"]

        rate_song(friend.id, song.id, 5)

        notifs = get_notifications(sharer.id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"


def test_self_rating_does_not_notify(app, sharer_and_song):
    """Rating your own song should not create a notification for yourself."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 4)

        assert get_notifications(sharer.id) == []
