import pytest

from sidestage.grounding.retrieval import GroundingTools
from sidestage.pipeline import Pipeline
from sidestage.store import seed


@pytest.fixture
def store(tmp_path):
    return seed(str(tmp_path / "test.db"))


@pytest.fixture
def tools(store):
    return GroundingTools(store)


@pytest.fixture
def pipeline(store):
    return Pipeline(store)
