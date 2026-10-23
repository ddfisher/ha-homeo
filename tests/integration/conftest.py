import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homeo.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def rules_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    d = tmp_path / "homeo"
    d.mkdir()
    return d


@pytest.fixture
def setup_homeo(hass, rules_dir):
    async def setup(files: dict[str, str] | None = None, options: dict | None = None):
        for name, text in (files or {}).items():
            (rules_dir / name).write_text(text)
        entry = MockConfigEntry(domain=DOMAIN, title="Homeo", options=options or {})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return setup
