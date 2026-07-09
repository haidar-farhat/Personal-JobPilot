"""Shared unit-test fixtures: isolated tmp SQLite engine/session.

Unit tests must never touch the real jobpilot.db. `tmp_session` builds a
fresh file-backed SQLite DB per test with the full schema.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base


@pytest.fixture()
def tmp_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'test.db'}", echo=False,
                            connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def tmp_session(tmp_engine):
    Session = sessionmaker(bind=tmp_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def session_factory(tmp_engine):
    """For monkeypatching a router module's get_session."""
    Session = sessionmaker(bind=tmp_engine)
    return lambda: Session()
