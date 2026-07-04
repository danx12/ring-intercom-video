"""Camera platform for Ring Intercom Video.

Two modes of operation:
1. LIVE STREAM (browser WebRTC) — user opens camera card in Lovelace,
   browser establishes WebRTC peer connection directly to Ring.
   This entity acts as signaling bridge only.

2. SNAPSHOT (server-side WebRTC) — camera.snapshot service or
   async_camera_image() triggers a server-side WebRTC connection
   using aiortc (run in an isolated venv, see venv_snapshot.py),
   captures a stabilized video frame, returns JPEG. Works from
   automations without needing a browser open.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    RTCIceCandidateInit,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from ring_doorbell.webrtcstream import RingWebRtcMessage

_LOGGER = logging.getLogger(__name__)

# Server-side snapshot capture settings
SNAPSHOT_CACHE_SECONDS = 10  # Don't re-capture within this window


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Ring Intercom camera entities."""
    ring_entries = hass.config_entries.async_entries("ring")
    if not ring_entries:
        _LOGGER.warning("Ring integration not configured")
        return

    entities = []
    for entry in ring_entries:
        ring_data = getattr(entry, "runtime_data", None)
        if ring_data is None:
            continue

        try:
            devices = ring_data.devices
            for device in devices.other:
                if device.kind == "intercom_handset_video":
                    _LOGGER.info(
                        "Found Ring Intercom Video: %s (id: %s)",
                        device.name,
                        device.device_api_id,
                    )
                    entities.append(RingIntercomCamera(device))
        except Exception:
            _LOGGER.exception("Error discovering Ring Intercom Video devices")

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ring Intercom Video camera(s)", len(entities))
    else:
        _LOGGER.info("No Ring Intercom Video devices found")


class RingIntercomCamera(Camera):
    """WebRTC live-stream camera + server-side snapshot for Ring Intercom Video."""

    def __init__(self, device) -> None:
        """Initialize the camera."""
        super().__init__()
        self._device = device
        self._attr_name = f"{device.name} Camera"
        self._attr_unique_id = f"ring_intercom_camera_{device.device_api_id}"
        self._attr_brand = "Ring"
        self._attr_model = "Intercom Video"
        self._attr_supported_features = CameraEntityFeature.STREAM

        # Snapshot cache
        self._last_image: bytes | None = None
        self._last_image_time: float = 0
        self._capturing: bool = False

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def motion_detection_enabled(self) -> bool:
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device.device_api_id,
            "device_kind": self._device.kind,
            "stream_method": "webrtc_native",
            "last_snapshot": self._last_image_time or None,
        }

    # ---- Snapshot (server-side WebRTC capture) ----

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Capture a snapshot via server-side WebRTC.

        Returns cached image if recent, otherwise starts a new
        WebRTC session with aiortc to grab a stabilized frame.
        """
        # Return cache if fresh
        if (
            self._last_image
            and (time.time() - self._last_image_time) < SNAPSHOT_CACHE_SECONDS
        ):
            return self._last_image

        # Avoid concurrent captures
        if self._capturing:
            return self._last_image

        self._capturing = True
        try:
            image = await self._capture_snapshot()
            if image and len(image) > 500:
                self._last_image = image
                self._last_image_time = time.time()
                _LOGGER.debug(
                    "Snapshot captured for %s (%d bytes)",
                    self._device.name,
                    len(image),
                )
        except Exception:
            _LOGGER.exception("Snapshot capture failed for %s", self._device.name)
        finally:
            self._capturing = False

        return self._last_image

    async def _capture_snapshot(self) -> bytes | None:
        """Server-side WebRTC snapshot, captured in an isolated venv.

        aiortc requires an `av` version range that can conflict with the
        exact `av` version some Home Assistant Core releases pin for their
        own camera/stream stack. To avoid touching HA's own environment, the
        actual WebRTC decode runs as a subprocess against a dedicated venv
        (see venv_snapshot.py) — only ticket-fetching, which needs this
        entity's authenticated Ring session, happens here.
        """
        import uuid

        from ring_doorbell.const import (
            APP_API_URI,
            RTC_STREAMING_TICKET_ENDPOINT,
            RTC_STREAMING_WEB_SOCKET_ENDPOINT,
        )

        from . import venv_snapshot

        config_dir = self.hass.config.config_dir

        if not await venv_snapshot.ensure_ready(config_dir):
            _LOGGER.debug(
                "Snapshot venv not ready — skipping this capture attempt "
                "(setup may still be in progress; see earlier log entries)."
            )
            return None

        try:
            resp = await self._device._ring.async_query(
                RTC_STREAMING_TICKET_ENDPOINT,
                method="POST",
                base_uri=APP_API_URI,
            )
            ticket = resp.json()["ticket"]
        except Exception:
            _LOGGER.debug("Failed to get WebRTC ticket", exc_info=True)
            return None

        ws_uri = RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), ticket)
        return await venv_snapshot.capture(
            config_dir, ws_uri, self._device.device_api_id
        )

    # ---- Live stream (browser WebRTC signaling bridge) ----

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle WebRTC offer from the HA frontend."""
        # Timing instrumentation: measure where the connection-setup
        # seconds go (Ring answer round-trip vs. ICE exchange). Enable
        # with: logger -> custom_components.ring_intercom_camera: debug
        t_start = time.monotonic()
        timing = {"answer": False, "candidate": False}

        def _ms() -> int:
            return int((time.monotonic() - t_start) * 1000)

        def _message_wrapper(ring_msg: RingWebRtcMessage) -> None:
            if ring_msg.error_code:
                msg = ring_msg.error_message or ""
                _LOGGER.debug(
                    "WebRTC %s: error after %dms: %s",
                    session_id,
                    _ms(),
                    msg,
                )
                send_message(WebRTCError(ring_msg.error_code, msg))
            elif ring_msg.answer:
                if not timing["answer"]:
                    timing["answer"] = True
                    _LOGGER.debug(
                        "WebRTC %s: Ring answer received after %dms",
                        session_id,
                        _ms(),
                    )
                send_message(WebRTCAnswer(ring_msg.answer))
            elif ring_msg.candidate:
                if not timing["candidate"]:
                    timing["candidate"] = True
                    _LOGGER.debug(
                        "WebRTC %s: first Ring ICE candidate after %dms",
                        session_id,
                        _ms(),
                    )
                send_message(
                    WebRTCCandidate(
                        RTCIceCandidateInit(
                            ring_msg.candidate,
                            sdp_m_line_index=ring_msg.sdp_m_line_index or 0,
                        )
                    )
                )

        _LOGGER.debug("WebRTC %s: offer received, contacting Ring", session_id)
        await self._device.generate_async_webrtc_stream(
            offer_sdp, session_id, _message_wrapper, keep_alive_timeout=None
        )
        _LOGGER.debug(
            "WebRTC %s: generate_async_webrtc_stream returned after %dms",
            session_id,
            _ms(),
        )

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward an ICE candidate from the browser to Ring."""
        if candidate.sdp_m_line_index is None:
            _LOGGER.warning("ICE candidate without sdp_m_line_index, ignoring")
            return
        await self._device.on_webrtc_candidate(
            session_id, candidate.candidate, candidate.sdp_m_line_index
        )

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session."""
        self._device.sync_close_webrtc_stream(session_id)
