"""Isolated-venv WebRTC snapshot capture.

aiortc requires an `av` version range that can conflict with the exact `av`
version some Home Assistant Core releases pin for their own camera/stream
stack (see README). Installing aiortc into HA's own environment risks
letting pip downgrade that shared `av` and breaking core streaming — or, if
the versions are mutually unsatisfiable, failing the whole integration's
requirements install.

To sidestep that entirely, aiortc + a compatible `av` are installed into a
dedicated venv under the HA config directory (never HA's own site-packages),
and the actual WebRTC capture runs as a subprocess using that venv's
interpreter (aiortc_worker.py). HA's own environment is never modified.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

VENV_DIRNAME = "ring_intercom_camera_venv"
WORKER_SCRIPT = Path(__file__).parent / "aiortc_worker.py"

_setup_lock = asyncio.Lock()
_setup_attempted = False
_setup_ok = False


def _venv_python(config_dir: str) -> Path:
    venv_dir = Path(config_dir) / VENV_DIRNAME
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


async def _run(*args: str, timeout_s: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout_s):
            _stdout, stderr = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stderr.decode(errors="replace")


async def ensure_ready(config_dir: str) -> bool:
    """Create the isolated venv and install aiortc into it, if needed.

    Idempotent and cheap to call repeatedly (e.g. before every capture
    attempt) — once the venv exists this just checks a path. Installation
    itself (first call only) can take a while since aiortc has native
    extensions; runs as a background subprocess so it never blocks the
    event loop.
    """
    global _setup_attempted, _setup_ok

    if _venv_python(config_dir).exists():
        return True

    async with _setup_lock:
        if _venv_python(config_dir).exists():
            return True
        if _setup_attempted:
            return _setup_ok
        _setup_attempted = True

        venv_dir = Path(config_dir) / VENV_DIRNAME
        _LOGGER.info(
            "Setting up isolated venv for WebRTC snapshot capture at %s "
            "(one-time; keeps aiortc's `av` pin separate from Home "
            "Assistant Core's own `av` — may take a minute or more)",
            venv_dir,
        )
        try:
            rc, err = await _run(
                sys.executable, "-m", "venv", str(venv_dir), timeout_s=120
            )
            if rc != 0:
                _LOGGER.warning("Failed to create snapshot venv: %s", err)
                return False

            python = _venv_python(config_dir)
            rc, err = await _run(
                str(python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "aiortc",
                "websockets>=12.0",
                "Pillow>=10.0.0",
                timeout_s=900,
            )
            if rc != 0:
                _LOGGER.warning("Failed to install snapshot venv dependencies: %s", err)
                return False
        except TimeoutError:
            _LOGGER.warning("Timed out setting up snapshot venv")
            return False
        except OSError:
            _LOGGER.exception("Error setting up snapshot venv")
            return False

        _LOGGER.info("Snapshot venv ready at %s", venv_dir)
        _setup_ok = True
        return True


async def capture(config_dir: str, ws_uri: str, device_api_id: int) -> bytes | None:
    """Run the WebRTC snapshot capture in the isolated venv.

    Returns JPEG bytes, or None if the venv isn't ready or capture failed.
    """
    python = _venv_python(config_dir)
    if not python.exists():
        return None

    request = json.dumps({"ws_uri": ws_uri, "device_api_id": device_api_id})
    proc = await asyncio.create_subprocess_exec(
        str(python),
        str(WORKER_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(request.encode()), timeout=30
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.debug("Snapshot worker timed out")
        return None

    if proc.returncode != 0:
        _LOGGER.debug(
            "Snapshot worker exited with %s: %s",
            proc.returncode,
            stderr.decode(errors="replace"),
        )
        return None

    try:
        response = json.loads(stdout.decode())
    except json.JSONDecodeError:
        _LOGGER.debug("Snapshot worker returned invalid output: %r", stdout[:200])
        return None

    if "error" in response:
        _LOGGER.debug("Snapshot worker reported: %s", response["error"])
        return None

    return base64.b64decode(response["image_b64"])
