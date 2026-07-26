import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import structlog
import websockets

from app.camera.base import CameraDevice
from app.camera.factory import create_camera
from app.config import get_settings
from app.environment.base import EnvironmentSensor
from app.environment.factory import create_environment_sensor
from app.heater.base import HeaterController, HeaterState
from app.heater.factory import create_heater_controller
from app.storage.paths import CAPTURES_DIR


logger = structlog.get_logger()
node_settings = get_settings()

camera: CameraDevice = create_camera(node_settings.camera_driver)
environment_sensor: EnvironmentSensor | None = None
heater: HeaterController | None = None
active_device_config: dict[str, Any] = {}
active_capture_settings: dict[str, Any] = {
    "interval_seconds": 60,
    # None means full sensor resolution; the server overrides these when the user
    # pins an explicit size.
    "full_resolution": True,
    "width": None,
    "height": None,
    "format": "jpg",
    "period": "night",
    "auto_exposure": False,
    "exposure_ms": 10000,
    "auto_gain": False,
    "gain": 8,
}
active_node_settings: dict[str, Any] = {}
active_sequence_task: asyncio.Task | None = None
active_sequence_id: str | None = None


def effective_node_settings():
    environment = active_device_config.get("environment", {})
    heater_config = active_device_config.get("heater", {})
    updates = {}

    if environment:
        if "driver" in environment:
            updates["environment_sensor_driver"] = environment["driver"]
        if "interval_seconds" in environment:
            updates["environment_interval_seconds"] = environment["interval_seconds"]
        if "bme280_i2c_bus" in environment:
            updates["bme280_i2c_bus"] = environment["bme280_i2c_bus"]
        if "bme280_i2c_address" in environment:
            updates["bme280_i2c_address"] = environment["bme280_i2c_address"]

    if heater_config:
        if "driver" in heater_config:
            updates["heater_driver"] = heater_config["driver"]
        if "gpio_pin" in heater_config:
            updates["heater_gpio_pin"] = heater_config["gpio_pin"]
        if "active_high" in heater_config:
            updates["heater_active_high"] = heater_config["active_high"]

    return node_settings.model_copy(update=updates)


def initialize_heater() -> HeaterController | None:
    settings = effective_node_settings()

    try:
        return create_heater_controller(settings)
    except Exception as error:
        logger.warning(
            "heater.initialization.failed",
            driver=settings.heater_driver,
            gpio_pin=settings.heater_gpio_pin,
            error=str(error),
        )
        return None


def reinitialize_heater() -> None:
    global heater

    if heater is not None:
        heater.close()

    heater = initialize_heater()


def reinitialize_environment_sensor() -> None:
    global environment_sensor

    environment_sensor = None


async def send_json(websocket, message: dict[str, Any]):
    await websocket.send(json.dumps(message))


async def upload_capture(sequence_id: str, capture_result) -> dict[str, Any]:
    metadata = capture_result.metadata or {}
    form_data = {
        "node_id": node_settings.node_id,
        "sequence_id": sequence_id,
        "format": capture_result.format,
        "metadata": json.dumps(metadata),
    }

    if capture_result.width is not None:
        form_data["width"] = str(capture_result.width)

    if capture_result.height is not None:
        form_data["height"] = str(capture_result.height)

    async with httpx.AsyncClient(timeout=60, trust_env=False, verify=False) as client:
        with Path(capture_result.file_path).open("rb") as capture_file:
            response = await client.post(
                node_settings.upload_url,
                headers=node_settings.api_headers,
                data=form_data,
                files={
                    "file": (
                        Path(capture_result.file_path).name,
                        capture_file,
                        "image/jpeg",
                    ),
                },
            )

    response.raise_for_status()
    return response.json()


async def send_hello(websocket):
    global heater

    if heater is None:
        heater = initialize_heater()

    settings = effective_node_settings()
    camera_info = camera.get_info()
    heater_state = heater.get_state() if heater else None
    message = {
        "type": "node.hello",
        "node_id": node_settings.node_id,
        "version": "0.1.0",
        "capabilities": {
            "camera": camera_info.driver,
            "camera_id": camera_info.camera_id,
            "camera_name": camera_info.name,
            "supports_exposure": camera_info.supports_exposure,
            "supports_gain": camera_info.supports_gain,
            "supported_formats": camera_info.supported_formats,
            "supports_jpg": "jpg" in camera_info.supported_formats,
            "environment_sensor": settings.environment_sensor_driver,
            "environment_interval_seconds": settings.environment_interval_seconds,
            "heater": heater_state.driver if heater_state else "disabled",
            "heater_gpio_pin": heater_state.gpio_pin if heater_state else None,
        },
    }

    if settings.environment_sensor_driver.lower() == "bme280":
        message["capabilities"]["bme280_i2c_bus"] = settings.bme280_i2c_bus
        message["capabilities"]["bme280_i2c_address"] = hex(settings.bme280_i2c_address_int)

    await send_json(websocket, message)
    logger.info("node.hello.sent", node_id=node_settings.node_id)


def heater_state_payload(state: HeaterState | None) -> dict[str, Any]:
    return {
        "enabled": bool(state.enabled) if state else False,
        "driver": state.driver if state else "disabled",
        "gpio_pin": state.gpio_pin if state else None,
    }


async def send_heater_state(websocket, state: HeaterState | None = None):
    if state is None and heater is not None:
        state = heater.get_state()

    await send_json(
        websocket,
        {
            "type": "heater.state",
            "node_id": node_settings.node_id,
            **heater_state_payload(state),
        },
    )


async def heartbeat_loop(websocket):
    while True:
        message = {
            "type": "node.heartbeat",
            "node_id": node_settings.node_id,
        }

        await send_json(websocket, message)
        logger.info("node.heartbeat.sent", node_id=node_settings.node_id)

        await asyncio.sleep(node_settings.heartbeat_interval_seconds)


async def environment_loop(websocket):
    global environment_sensor

    settings = effective_node_settings()

    if settings.environment_sensor_driver.lower() in {"none", "disabled", "off"}:
        logger.info("environment.disabled")
        return

    while True:
        try:
            settings = effective_node_settings()

            if settings.environment_sensor_driver.lower() in {"none", "disabled", "off"}:
                environment_sensor = None
                await asyncio.sleep(max(1, settings.environment_interval_seconds))
                continue

            if environment_sensor is None:
                environment_sensor = create_environment_sensor(settings)

            if environment_sensor is None:
                return

            reading = await asyncio.to_thread(environment_sensor.read)
            payload = reading.to_message_payload()

            await send_json(
                websocket,
                {
                    "type": "environment.telemetry",
                    "node_id": node_settings.node_id,
                    "sensor": environment_sensor.driver,
                    **payload,
                },
            )
            logger.info(
                "environment.telemetry.sent",
                node_id=node_settings.node_id,
                sensor=environment_sensor.driver,
                temperature_c=payload["temperature_c"],
                humidity_percent=payload["humidity_percent"],
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            logger.warning(
                "environment.read.failed",
                driver=settings.environment_sensor_driver,
                error=str(error),
            )
            environment_sensor = None

        await asyncio.sleep(max(1, settings.environment_interval_seconds))


def effective_capture_settings(sequence_overrides: dict[str, Any]) -> dict[str, Any]:
    """The server's current config, with this sequence's own overrides on top.

    Rebuilt for every frame. A running sequence used to keep the settings dict it
    was started with, so a config update went into `active_capture_settings` and
    sat there unread: turning auto exposure off, changing the interval, or the
    day/night switchover all did nothing until capture was stopped and started
    again.
    """
    return {**active_capture_settings, **sequence_overrides}


async def capture_sequence_loop(websocket, sequence_id: str, sequence_overrides: dict[str, Any]):
    capture_settings = effective_capture_settings(sequence_overrides)
    startup_interval_seconds = 1.0
    startup_deadline = asyncio.get_running_loop().time() + 10.0
    startup_stable_frames = 0
    startup_burst_active = bool(capture_settings.get("period") == "day") and (
        bool(capture_settings.get("auto_exposure", False))
        or bool(capture_settings.get("auto_gain", False))
    )
    next_capture_at = asyncio.get_running_loop().time()

    logger.info(
        "sequence.loop.started",
        sequence_id=sequence_id,
        interval_seconds=capture_settings.get("interval_seconds"),
        settings=capture_settings,
        overrides=sequence_overrides,
    )

    try:
        while True:
            now = asyncio.get_running_loop().time()

            if now < next_capture_at:
                await asyncio.sleep(next_capture_at - now)

            previous_settings = capture_settings
            capture_settings = effective_capture_settings(sequence_overrides)
            interval_seconds = int(capture_settings.get("interval_seconds") or 30)

            if capture_settings != previous_settings:
                logger.info(
                    "sequence.settings.refreshed",
                    sequence_id=sequence_id,
                    period=capture_settings.get("period"),
                    interval_seconds=interval_seconds,
                    auto_exposure=capture_settings.get("auto_exposure"),
                    auto_gain=capture_settings.get("auto_gain"),
                    exposure_ms=capture_settings.get("exposure_ms"),
                    max_exposure_ms=capture_settings.get("max_exposure_ms"),
                    max_gain=capture_settings.get("max_gain"),
                )

            capture_started_at = asyncio.get_running_loop().time()
            active_interval_seconds = (
                startup_interval_seconds
                if startup_burst_active and capture_started_at < startup_deadline
                else interval_seconds
            )
            next_capture_at = capture_started_at + active_interval_seconds

            await send_json(
                websocket,
                {
                    "type": "capture.started",
                    "node_id": node_settings.node_id,
                    "sequence_id": sequence_id,
                },
            )

            capture_result = await camera.capture(settings=capture_settings, output_dir=CAPTURES_DIR)

            logger.info(
                "capture.saved.local",
                sequence_id=sequence_id,
                path=str(capture_result.file_path),
                format=capture_result.format,
                width=capture_result.width,
                height=capture_result.height,
            )

            await send_json(
                websocket,
                {
                    "type": "capture.saved.local",
                    "node_id": node_settings.node_id,
                    "sequence_id": sequence_id,
                    "path": str(capture_result.file_path),
                    "format": capture_result.format,
                    "width": capture_result.width,
                    "height": capture_result.height,
                    "metadata": capture_result.metadata or {},
                },
            )

            upload_result = await upload_capture(
                sequence_id=sequence_id,
                capture_result=capture_result,
            )

            capture_metadata = capture_result.metadata or {}

            if startup_burst_active:
                if capture_metadata.get("mean_within_threshold"):
                    startup_stable_frames += 1
                else:
                    startup_stable_frames = 0

                logger.info(
                    "sequence.startup.convergence",
                    sequence_id=sequence_id,
                    frame_index=capture_metadata.get("frame_index"),
                    mean=capture_metadata.get("mean"),
                    actual_exposure_ms=capture_metadata.get("actual_exposure_ms"),
                    actual_gain=capture_metadata.get("actual_analogue_gain"),
                    next_exposure_ms=capture_metadata.get("next_exposure_ms"),
                    next_gain=capture_metadata.get("next_gain"),
                    stable_frames=startup_stable_frames,
                    active_interval_seconds=active_interval_seconds,
                    burst_active=True,
                )

                if startup_stable_frames >= 2 or capture_started_at >= startup_deadline:
                    startup_burst_active = False
                    logger.info(
                        "sequence.startup.convergence.complete",
                        sequence_id=sequence_id,
                        stable_frames=startup_stable_frames,
                        elapsed_seconds=round(capture_started_at - (startup_deadline - 10.0), 3),
                        next_interval_seconds=interval_seconds,
                    )

            if upload_result.get("status") == "skipped":
                with suppress(FileNotFoundError):
                    Path(capture_result.file_path).unlink()

                logger.info(
                    "capture.skipped",
                    sequence_id=sequence_id,
                    reason=upload_result.get("reason"),
                    period=upload_result.get("period"),
                )

                await send_json(
                    websocket,
                    {
                        "type": "capture.skipped",
                        "node_id": node_settings.node_id,
                        "sequence_id": sequence_id,
                        "reason": upload_result.get("reason"),
                        "period": upload_result.get("period"),
                    },
                )
                continue

            logger.info(
                "capture.uploaded",
                sequence_id=sequence_id,
                capture_id=upload_result.get("capture_id"),
                server_path=upload_result.get("path"),
            )

            await send_json(
                websocket,
                {
                    "type": "capture.uploaded",
                    "node_id": node_settings.node_id,
                    "sequence_id": sequence_id,
                    "capture_id": upload_result.get("capture_id"),
                    "server_path": upload_result.get("path"),
                    "size_bytes": upload_result.get("size_bytes"),
                },
            )

            finished_at = asyncio.get_running_loop().time()
            overrun_seconds = finished_at - next_capture_at

            if overrun_seconds > 0:
                logger.warning(
                    "capture.cadence.overrun",
                    sequence_id=sequence_id,
                    interval_seconds=interval_seconds,
                    overrun_seconds=round(overrun_seconds, 3),
                )

    except asyncio.CancelledError:
        logger.warning("sequence.loop.cancelled", sequence_id=sequence_id)
        raise

    except Exception as error:
        logger.exception(
            "sequence.loop.failed",
            sequence_id=sequence_id,
            error=str(error),
        )

        await send_json(
            websocket,
            {
                "type": "sequence.failed",
                "node_id": node_settings.node_id,
                "sequence_id": sequence_id,
                "error": str(error),
            },
        )


async def start_sequence(
    websocket,
    sequence_id: str,
    capture_settings: dict[str, Any],
    overrides: dict[str, Any] | None = None,
):
    global active_capture_settings, active_sequence_id, active_sequence_task

    if active_sequence_task is not None and not active_sequence_task.done():
        await stop_sequence(websocket, reason="replaced")

    # The start message carries the full period profile plus whatever the caller
    # overrode for this one sequence. Only the overrides belong to the sequence;
    # the profile is folded into the shared config, so a later config.update -
    # a settings change, or the day/night switchover - supersedes it.
    sequence_overrides = dict(overrides or {})
    active_capture_settings = {
        **active_capture_settings,
        **(capture_settings or {}),
    }

    await send_json(
        websocket,
        {
            "type": "sequence.started",
            "node_id": node_settings.node_id,
            "sequence_id": sequence_id,
        },
    )

    active_sequence_id = sequence_id
    active_sequence_task = asyncio.create_task(
        capture_sequence_loop(
            websocket=websocket,
            sequence_id=sequence_id,
            sequence_overrides=sequence_overrides,
        )
    )


async def apply_config_update(websocket, message: dict[str, Any]):
    global active_capture_settings, active_node_settings

    active_node_settings = message.get("settings", {}) or {}
    active_capture_settings = {
        **active_capture_settings,
        **(message.get("active_settings", {}) or {}),
    }

    logger.info(
        "config.updated",
        current_period=message.get("current_period"),
        active_settings=active_capture_settings,
    )

    await send_json(
        websocket,
        {
            "type": "config.updated",
            "node_id": node_settings.node_id,
            "current_period": message.get("current_period"),
        },
    )

    capture_enabled = bool(message.get("capture_enabled", False))
    desired_sequence_id = message.get("sequence_id")

    sequence_running = (
        active_sequence_task is not None
        and not active_sequence_task.done()
        and active_sequence_id == desired_sequence_id
    )

    if capture_enabled and desired_sequence_id and not sequence_running:
        await start_sequence(
            websocket=websocket,
            sequence_id=desired_sequence_id,
            capture_settings=active_capture_settings,
        )

    # A sequence that is already running needs no restart: its loop rebuilds the
    # settings from active_capture_settings before every frame, so the update
    # lands on the next capture instead of discarding the one in flight.

    elif not capture_enabled and active_sequence_task is not None and not active_sequence_task.done():
        await stop_sequence(websocket=websocket, reason="config_disabled")


async def stop_sequence(
    websocket,
    sequence_id: str | None = None,
    reason: str = "requested",
):
    global active_sequence_id, active_sequence_task

    if active_sequence_task is None or active_sequence_task.done():
        await send_json(
            websocket,
            {
                "type": "sequence.stop.ignored",
                "node_id": node_settings.node_id,
                "sequence_id": sequence_id,
                "reason": "no_active_sequence",
            },
        )
        return

    stopped_sequence_id = active_sequence_id

    if sequence_id is not None and sequence_id != active_sequence_id:
        await send_json(
            websocket,
            {
                "type": "sequence.stop.ignored",
                "node_id": node_settings.node_id,
                "sequence_id": sequence_id,
                "active_sequence_id": active_sequence_id,
                "reason": "sequence_id_mismatch",
            },
        )
        return

    active_sequence_task.cancel()

    with suppress(asyncio.CancelledError):
        await active_sequence_task

    active_sequence_task = None
    active_sequence_id = None

    await send_json(
        websocket,
        {
            "type": "sequence.stopped",
            "node_id": node_settings.node_id,
            "sequence_id": stopped_sequence_id,
            "reason": reason,
        },
    )


async def receive_loop(websocket):
    global active_device_config, heater

    async for raw_message in websocket:
        message = json.loads(raw_message)
        message_type = message.get("type")

        logger.info(
            "server.message.received",
            message_type=message_type,
            message=message,
        )

        if message_type == "sequence.start":
            sequence_id = message.get("sequence_id")
            capture_settings = message.get("settings", {})

            if not sequence_id:
                logger.warning("sequence.start.invalid", reason="missing_sequence_id")
                continue

            logger.info(
                "sequence.start.received",
                sequence_id=sequence_id,
                settings=capture_settings,
                overrides=message.get("overrides"),
            )

            await start_sequence(
                websocket=websocket,
                sequence_id=sequence_id,
                capture_settings=capture_settings,
                overrides=message.get("overrides"),
            )

        elif message_type == "sequence.stop":
            await stop_sequence(
                websocket=websocket,
                sequence_id=message.get("sequence_id"),
            )

        elif message_type == "config.update":
            await apply_config_update(websocket=websocket, message=message)

        elif message_type == "device.config":
            active_device_config = (message.get("settings", {}) or {}).get("devices", {}) or {}
            reinitialize_environment_sensor()
            reinitialize_heater()
            logger.info("device.config.updated", devices=active_device_config)
            await send_json(
                websocket,
                {
                    "type": "device.configured",
                    "node_id": node_settings.node_id,
                    "devices": active_device_config,
                    "heater": heater_state_payload(heater.get_state() if heater else None),
                },
            )

        elif message_type == "heater.set":
            requested_enabled = bool(message.get("enabled", False))

            if heater is None:
                heater = initialize_heater()

            if heater is None:
                await send_heater_state(websocket, None)
                continue

            state = heater.set_enabled(requested_enabled)
            logger.info(
                "heater.updated",
                enabled=state.enabled,
                driver=state.driver,
                gpio_pin=state.gpio_pin,
            )
            await send_heater_state(websocket, state)


async def cancel_active_sequence(reason: str):
    global active_sequence_id, active_sequence_task

    if active_sequence_task is None or active_sequence_task.done():
        active_sequence_task = None
        active_sequence_id = None
        return

    logger.warning(
        "sequence.local.cancelled",
        sequence_id=active_sequence_id,
        reason=reason,
    )

    active_sequence_task.cancel()

    with suppress(asyncio.CancelledError):
        await active_sequence_task

    active_sequence_task = None
    active_sequence_id = None


async def run_connection():
    logger.info(
        "node.connecting",
        node_id=node_settings.node_id,
        server_url=node_settings.server_ws_url,
    )

    async with websockets.connect(
        node_settings.server_ws_url,
        additional_headers=node_settings.api_headers,
    ) as websocket:
        logger.info("node.connected", node_id=node_settings.node_id)

        await send_hello(websocket)

        try:
            heartbeat_task = asyncio.create_task(heartbeat_loop(websocket))
            environment_task = asyncio.create_task(environment_loop(websocket))
            receive_task = asyncio.create_task(receive_loop(websocket))
            tasks = {heartbeat_task, environment_task, receive_task}

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

            for task in pending:
                task.cancel()

            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task

            for task in done:
                task.result()

        finally:
            await cancel_active_sequence(reason="connection_lost")


async def main():
    reconnect_delay = node_settings.reconnect_initial_delay_seconds

    logger.info(
        "node.starting",
        node_id=node_settings.node_id,
        server_url=node_settings.server_ws_url,
    )

    while True:
        try:
            await run_connection()
            logger.warning(
                "node.connection.closed",
                node_id=node_settings.node_id,
                reconnect_delay_seconds=reconnect_delay,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = node_settings.reconnect_initial_delay_seconds

        except Exception as error:
            logger.warning(
                "node.connection.lost",
                node_id=node_settings.node_id,
                error=str(error),
                reconnect_delay_seconds=reconnect_delay,
            )

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(
                reconnect_delay * 2,
                node_settings.reconnect_max_delay_seconds,
            )


if __name__ == "__main__":
    asyncio.run(main())
