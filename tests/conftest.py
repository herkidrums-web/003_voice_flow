"""Shared test fixtures."""
import pytest

import config as config_module
from src import notion_writer as notion_writer_module
from src import summarizer as summarizer_module


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset module-level singletons between tests."""
    config_module._settings = None
    summarizer_module._client = None
    notion_writer_module._client = None
    yield
    config_module._settings = None
    summarizer_module._client = None
    notion_writer_module._client = None
