"""Test helpers. All payloads come from signalpost.offline_fixtures (SYNTHETIC, labelled)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from signalpost.agent import Agent  # noqa: E402
from signalpost.config import Settings  # noqa: E402
from signalpost.http import BudgetedClient  # noqa: E402
from signalpost.offline_fixtures import FakeSession  # noqa: E402
from signalpost.storage import Store  # noqa: E402


def make(session=None, **settings):
    s = Settings(min_interval=0.0, backoff_base=0.0, max_retries=settings.pop("max_retries", 1), **settings)
    sess = session or FakeSession()
    agent = Agent(s, Store(":memory:"), BudgetedClient(s, session=sess, cache=None, sleep=lambda _x: None))
    return agent, sess


@pytest.fixture
def agent_and_session():
    return make()
