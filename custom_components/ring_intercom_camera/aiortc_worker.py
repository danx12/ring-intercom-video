"""Standalone WebRTC snapshot-capture worker.

Runs inside the isolated venv created by venv_snapshot.py — NOT inside Home
Assistant's own Python process. It only ever imports third-party packages
installed into that dedicated venv (aiortc, its own compatible `av`,
websockets, Pillow); it must not import `homeassistant` or `ring_doorbell`,
since those aren't installed there.

Protocol: reads one JSON object from stdin — {"ws_uri": str,
"device_api_id": int} — and writes one JSON object to stdout —
{"image_b64": str} on success or {"error": str} on failure.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import ssl
import sys
import uuid
from io import BytesIO

SNAPSHOT_MAX_FRAMES = 75  # Max frames to examine (~3s at 25fps)
SNAPSHOT_BRIGHTNESS_THRESHOLD = 25  # Min brightness to consider "real" video
SNAPSHOT_STABILIZE_FRAMES = 5  # Consecutive bright frames before capture


async def _capture(ws_uri: str, device_api_id: int) -> bytes | None:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from websockets.asyncio.client import connect as ws_connect

    pc = RTCPeerConnection()
    snapshot_data: dict[str, bytes | None] = {"image": None}
    capture_done = asyncio.Event()

    @pc.on("track")
    async def on_track(track):
        if track.kind != "video":
            return

        frame_count = 0
        best_frame = None
        best_brightness = 0.0
        bright_streak = 0
        prev_brightness = 0.0

        try:
            while frame_count < SNAPSHOT_MAX_FRAMES:
                frame = await asyncio.wait_for(track.recv(), timeout=10)
                frame_count += 1

                img = frame.to_image()
                w, h = img.size
                points = [
                    (w // 4, h // 4),
                    (w // 2, h // 4),
                    (3 * w // 4, h // 4),
                    (w // 4, h // 2),
                    (w // 2, h // 2),
                    (3 * w // 4, h // 2),
                    (w // 4, 3 * h // 4),
                    (w // 2, 3 * h // 4),
                    (3 * w // 4, 3 * h // 4),
                ]
                total = sum(sum(img.getpixel(p)) / 3 for p in points)
                brightness = total / len(points)

                if brightness > best_brightness:
                    best_brightness = brightness
                    best_frame = img

                if brightness > SNAPSHOT_BRIGHTNESS_THRESHOLD:
                    bright_streak += 1
                    if (
                        bright_streak >= SNAPSHOT_STABILIZE_FRAMES
                        and prev_brightness > 0
                        and abs(brightness - prev_brightness) < brightness * 0.15
                    ):
                        best_frame = img
                        break
                else:
                    bright_streak = 0

                prev_brightness = brightness
        except TimeoutError:
            pass
        except Exception:
            pass

        if best_frame:
            buf = BytesIO()
            best_frame.save(buf, "JPEG", quality=85)
            snapshot_data["image"] = buf.getvalue()

        capture_done.set()

    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    dialog_id = str(uuid.uuid4())
    session_id = None
    ssl_ctx = ssl.create_default_context()

    try:
        async with ws_connect(
            ws_uri, user_agent_header="android:com.ringapp", ssl=ssl_ctx
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "method": "live_view",
                        "dialog_id": dialog_id,
                        "body": {
                            "doorbot_id": device_api_id,
                            "stream_options": {
                                "audio_enabled": False,
                                "video_enabled": True,
                            },
                            "sdp": pc.localDescription.sdp,
                            "type": "offer",
                        },
                    }
                )
            )

            loop = asyncio.get_event_loop()
            start = loop.time()
            while loop.time() - start < 20 and not capture_done.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    msg = json.loads(raw)
                    method = msg.get("method", "")
                    body = msg.get("body", {})

                    if method == "sdp":
                        sdp = body.get("sdp", "")
                        if sdp:
                            await pc.setRemoteDescription(
                                RTCSessionDescription(sdp=sdp, type="answer")
                            )
                    elif method == "session_created":
                        session_id = body.get("session_id")
                    elif (
                        method == "notification"
                        and body.get("text") == "camera_connected"
                    ):
                        if session_id:
                            await ws.send(
                                json.dumps(
                                    {
                                        "method": "activate_session",
                                        "dialog_id": dialog_id,
                                        "body": {
                                            "doorbot_id": device_api_id,
                                            "session_id": session_id,
                                        },
                                    }
                                )
                            )
                    elif method == "close":
                        break
                except TimeoutError:
                    if capture_done.is_set():
                        break

            with contextlib.suppress(Exception):
                await ws.send(
                    json.dumps(
                        {
                            "method": "close",
                            "dialog_id": dialog_id,
                            "body": {
                                "session_id": session_id or "",
                                "reason": {"code": 0, "text": ""},
                            },
                        }
                    )
                )
    except Exception:
        pass
    finally:
        await pc.close()

    return snapshot_data["image"]


def main() -> None:
    request = json.loads(sys.stdin.read())
    try:
        image = asyncio.run(_capture(request["ws_uri"], request["device_api_id"]))
    except Exception as exc:
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        return

    if image:
        json.dump({"image_b64": base64.b64encode(image).decode("ascii")}, sys.stdout)
    else:
        json.dump({"error": "no_frame_captured"}, sys.stdout)


if __name__ == "__main__":
    main()
