"""Integration-loading smoke test against an in-memory, non-live HA core.

Uses pytest-homeassistant-custom-component's `hass` fixture — a real
Home Assistant core instance running purely in-process, with no network
I/O or live server involved.
"""

from homeassistant.setup import async_setup_component

from custom_components.ring_intercom_camera import DOMAIN


async def test_setup_without_ring_config_entry(hass):
    """Component must load cleanly even with no Ring integration configured."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()

    # No Ring config entry present -> no intercom devices -> no camera entities,
    # but setup itself must not raise.
    assert hass.states.async_entity_ids("camera") == []
