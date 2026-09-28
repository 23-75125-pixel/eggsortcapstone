"""Background serial integration for the EggSort ESP32 controller."""

from __future__ import annotations

import os
import re
from collections import deque
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Any, Callable
from egg_standards import classify_egg_size, servo_command


EventHandler = Callable[[dict[str, Any]], None]


class Esp32ProtocolParser:
    """Parse the human-readable EggSort ESP32 serial protocol."""

    READING = re.compile(r"Reading\s+(\d+)\s*:\s*(-?\d+)\s*g", re.I)
    FINAL_WEIGHT = re.compile(r"FINAL WEIGHT\s*:\s*(-?\d+)\s*g", re.I)
    SIZE = re.compile(r"SIZE\s*:\s*([A-Z _]+)", re.I)
    SORTED = re.compile(r"SERVO SORTED\s*:\s*([A-Z _]+)", re.I)
    LIVE_WEIGHT = re.compile(r"LIVE WEIGHT\s*:\s*(-?\d+)\s*g", re.I)
    HX711_READY = re.compile(r"HX711 READY\s*:\s*(YES|NO)", re.I)
    PCA9685_READY = re.compile(r"PCA9685 READY\s*:\s*(YES|NO)", re.I)
    CONTROLLER_STATE = re.compile(r"CONTROLLER STATE\s*:\s*(.+)", re.I)

    def __init__(self) -> None:
        self.final_weight: int | None = None
        self.readings: list[int] = []

    def parse(self, line: str) -> list[dict[str, Any]]:
        clean = line.strip()
        if not clean or set(clean) == {"="}:
            return []

        lowered = clean.lower()
        if lowered == "egg sorting ready":
            return [{"type": "ready", "message": clean}]
        hx711_ready = self.HX711_READY.fullmatch(clean)
        if hx711_ready:
            return [{
                "type": "hx711_status",
                "ready": hx711_ready.group(1).upper() == "YES",
                "message": clean,
            }]
        pca9685_ready = self.PCA9685_READY.fullmatch(clean)
        if pca9685_ready:
            return [{
                "type": "pca9685_status",
                "ready": pca9685_ready.group(1).upper() == "YES",
                "message": clean,
            }]
        live_weight = self.LIVE_WEIGHT.fullmatch(clean)
        if live_weight:
            return [{
                "type": "load_cell_status",
                "weight_grams": int(live_weight.group(1)),
                "message": clean,
            }]
        controller_state = self.CONTROLLER_STATE.fullmatch(clean)
        if controller_state:
            return [{
                "type": "controller_state",
                "state": controller_state.group(1).strip(),
                "message": clean,
            }]
        if lowered == "egg detected":
            self.final_weight = None
            self.readings = []
            return [{"type": "egg_detected", "message": clean}]
        if lowered == "egg left":
            # Removal is never treated as a completed measurement. Only the
            # controller's explicit FINAL WEIGHT + SIZE pair may advance an
            # egg to sorting and persistence.
            self.final_weight = None
            self.readings = []
            return [{"type": "egg_left", "message": clean}]

        reading = self.READING.fullmatch(clean)
        if reading:
            value = int(reading.group(2))
            self.readings = [*self.readings[-2:], value]
            return [{
                "type": "weight_reading",
                "reading_number": int(reading.group(1)),
                "weight_grams": value,
                "message": clean,
            }]

        final_weight = self.FINAL_WEIGHT.fullmatch(clean)
        if final_weight:
            self.final_weight = int(final_weight.group(1))
            return [{
                "type": "final_weight",
                "weight_grams": self.final_weight,
                "message": clean,
            }]

        size = self.SIZE.fullmatch(clean)
        if size:
            event = {
                "type": "egg_complete",
                "weight_grams": self.final_weight,
                "size": size.group(1).strip().replace("_", " ").title(),
                "readings": self.readings.copy(),
                "message": clean,
            }
            self.final_weight = None
            self.readings = []
            return [event]

        sorted_size = self.SORTED.fullmatch(clean)
        if sorted_size:
            return [{
                "type": "sort_complete",
                "size": sorted_size.group(1).strip().replace("_", " ").title(),
                "message": clean,
            }]

        return [{"type": "serial_message", "message": clean}]

    @staticmethod
    def _classify_size(weight_grams: int) -> str:
        return classify_egg_size(weight_grams)


class Esp32Bridge:
    """Maintain a reconnecting controller link without blocking Flask."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._serial: Any | None = None
        self._handler: EventHandler | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._running = False
        self._connected = False
        self._port: str | None = None
        self._error: str | None = None
        self._last_command: str | None = None
        self._diagnostics: dict[str, Any] = {
            "hx711_ready": None,
            "pca9685_ready": None,
            "live_weight_grams": None,
            "controller_state": None,
        }
        self.baud_rate = int(os.environ.get("ESP32_BAUD_RATE", "115200"))

    def set_event_handler(self, handler: EventHandler) -> None:
        self._handler = handler

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.status()
            self._stop_event.clear()
            self._running = True
            self._error = None
            self._thread = Thread(
                target=self._read_loop,
                name="eggsort-esp32-reader",
                daemon=True,
            )
            self._thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            serial_connection = self._serial
        if serial_connection is not None:
            try:
                serial_connection.close()
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=3)
        with self._lock:
            self._running = False
            self._connected = False
            self._thread = None
            self._serial = None
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "connected": self._connected,
                "port": self._port,
                "baud_rate": self.baud_rate,
                "controller": "ESP32",
                "error": self._error,
                "last_command": self._last_command,
                "diagnostics": dict(self._diagnostics),
                "latest_event": self._events[-1] if self._events else None,
            }

    def sort_egg(self, size: str) -> str:
        command = servo_command(size)
        self._send_command(command)
        self._publish({
            "type": "servo_command",
            "size": size,
            "message": command,
        })
        return command

    def measure_egg(self, quality: str) -> str:
        quality_code = quality.upper().replace(" ", "_")
        if quality_code not in {"CRACK", "GOOD", "ROTTEN", "UNDEFINED"}:
            raise ValueError(f"Unsupported egg quality: {quality}")
        command = f"MEASURE:{quality_code}"
        self._send_command(command)
        self._publish({
            "type": "measurement_command",
            "quality": quality,
            "message": command,
        })
        return command

    def publish_status(self, message: str, event_type: str = "flow_status") -> None:
        """Expose application-coordinator state in the hardware status feed."""
        self._publish({"type": event_type, "message": message})

    def _send_command(self, command: str) -> None:
        with self._lock:
            connection = self._serial
            if not self._connected or connection is None:
                raise RuntimeError(
                    "The ESP32 controller is not connected; command "
                    "was not sent."
                )
            try:
                connection.write(f"{command}\n".encode("ascii"))
                connection.flush()
                self._last_command = command
            except Exception as exc:
                self._connected = False
                self._error = str(exc)
                try:
                    connection.close()
                except Exception:
                    pass
                raise RuntimeError(
                    f"Unable to send {command} to the ESP32: {exc}"
                ) from exc

    def advance_gate(self) -> None:
        self._send_command("ADVANCE")
        self._publish({
            "type": "gate_command",
            "message": "ADVANCE",
        })

    def _find_port(self) -> str:
        configured = os.environ.get("ESP32_PORT")
        if configured:
            return configured

        from serial.tools import list_ports

        ports = list(list_ports.comports())
        controller_markers = (
            "esp32",
            "cp210",
            "ch340",
            "ch341",
            "usb serial",
            "silicon labs",
        )
        candidates = []
        for port in ports:
            description = (
                f"{port.description} {port.manufacturer or ''} "
                f"{port.hwid or ''}"
            ).lower()
            if any(marker in description for marker in controller_markers):
                candidates.append(port.device)
        if not candidates and len(ports) == 1:
            candidates = [ports[0].device]
        if not candidates:
            raise RuntimeError(
                "No ESP32 controller found. Connect it by USB or set "
                "ESP32_PORT (for example COM5)."
            )
        return candidates[0]

    def _read_loop(self) -> None:
        parser = Esp32ProtocolParser()
        while not self._stop_event.is_set():
            try:
                import serial

                port = self._find_port()
                connection = serial.Serial(
                    port,
                    self.baud_rate,
                    timeout=0.5,
                )
                with self._lock:
                    self._serial = connection
                    self._port = port
                    self._connected = True
                    self._error = None

                while not self._stop_event.is_set():
                    raw = connection.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    for event in parser.parse(line):
                        self._publish(event)
                        if event.get("type") == "ready":
                            connection.write(b"STATUS\n")
                            connection.flush()
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._serial = None
                    self._error = str(exc)
                if not self._stop_event.wait(2):
                    continue
            finally:
                with self._lock:
                    connection = self._serial
                    self._serial = None
                    self._connected = False
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

        with self._lock:
            self._running = False

    def _publish(self, event: dict[str, Any]) -> None:
        event = {
            **event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            event_type = event.get("type")
            if event_type == "hx711_status":
                self._diagnostics["hx711_ready"] = event.get("ready")
            elif event_type == "pca9685_status":
                self._diagnostics["pca9685_ready"] = event.get("ready")
            elif event_type == "load_cell_status":
                self._diagnostics["live_weight_grams"] = event.get(
                    "weight_grams"
                )
            elif event_type == "controller_state":
                self._diagnostics["controller_state"] = event.get("state")
            self._events.append(event)
        if self._handler is not None:
            self._handler(event)


ESP32_BRIDGE = Esp32Bridge()
