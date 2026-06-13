import pytest

from warden.config import Config
from warden.notifier.logchannel import LogChannel
from warden.store import Store


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        mode="active",
        state_dir=tmp_path / "state",
        incidents_dir=tmp_path / "incidents",
        sonarr_api_key="sonarr-secret-key-123",
        transmission_pass="hunter2-password",
    )
    cfg.state_dir.mkdir(parents=True)
    return cfg


@pytest.fixture
def store(config):
    return Store(config.state_dir / "warden.db")


@pytest.fixture
def channel(config):
    return LogChannel(config)
