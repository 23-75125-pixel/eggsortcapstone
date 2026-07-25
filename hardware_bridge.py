"""Background serial integration for the EggSort Arduino controllers."""

from __future__ import annotations

import os
import re
from collections import deque
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from time import sleep
from typing import Any, Callable
from egg_standards import classify_egg_size, servo_command


EventHandler = Callable[[dict[str, Any]], None]


class ArduinoProtocolParser:
    """Parse the existing human-readable load-cell sketch output."""

    READING = re.compile(r"Reading\s+(\d+)\s*:\s*(-?\d+)\s*g", re.I)
    FINAL_WEIGHT = re.compile(r"FINAL WEIGHT\s*:\s*(-?\d+)\s*g", re.I)
    SIZE = re.compile(r"SIZE\s*:\s*([A-Z _]+)", re.I)

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
        if lowered == "egg detected":
            self.final_weight = None
            self.readings = []
            return [{"type": "egg_detected", "message": clean}]
        if lowered == "egg left":
            events: list[dict[str, Any]] = []
            # The current Arduino sketch only emits FINAL WEIGHT/SIZE for
            # Small and Large. Persist other stable weights from its last
            # three readings so Medium, Extra Large, and Jumbo are not lost.
            if self.readings:
                recent = self.readings[-3:]
                weight = round(sum(recent) / len(recent))
                events.append({
                    "type": "egg_complete",
                    "weight_grams": weight,
                    "size": self._classify_size(weight),
                    "readings": recent,
                    "message": f"Calculated final weight: {weight} g",
                })
            self.final_weight = None
            self.readings = []
            events.append({"type": "egg_left", "message": clean})
            return events

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

        if lowered in {"servo running...", "servo stopped"}:
            return [{"type": "stopper_status", "message": clean}]
        return [{"type": "serial_message", "message": clean}]

    @staticmethod
    def _classify_size(weight_grams: int) -> str:
        return classify_egg_size(weight_grams)


class ArduinoBridge:
    """Maintain a reconnecting serial reader without blocking Flask."""

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
        self.baud_rate = int(os.environ.get("ARDUINO_BAUD_RATE", "9600"))

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
                name="eggsort-arduino-reader",
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
                "error": self._error,
                "last_command": self._last_command,
                "latest_event": self._events[-1] if self._events else None,
            }

    def sort_egg(self, size: str) -> str:
        command = servo_command(size)
        with self._lock:
            connection = self._serial
            if not self._connected or connection is None:
                raise RuntimeError(
                    "The load-cell Arduino is not connected; servo command "
                    "was not sent."
                )
            connection.write(f"{command}\n".encode("ascii"))
            connection.flush()
            self._last_command = command
        self._publish({
            "type": "servo_command",
            "size": size,
            "message": command,
        })
        return command

    def trigger_stopper(self) -> None:
        stopper_port = os.environ.get("ARDUINO_STOPPER_PORT")
        if not stopper_port:
            raise RuntimeError(
                "Set ARDUINO_STOPPER_PORT to the COM port running "
                "stooper-servo-loadcell.ino."
            )
        try:
            import serial

            with serial.Serial(
                stopper_port,
                self.baud_rate,
                timeout=1,
                write_timeout=1,
            ) as connection:
                sleep(2)
                connection.write(b"start\n")
                connection.flush()
        except Exception as exc:
            raise RuntimeError(
                f"Unable to trigger stopper on {stopper_port}: {exc}"
            ) from exc

    def _find_port(self) -> str:
        configured = os.environ.get("ARDUINO_LOADCELL_PORT")
        if configured:
            return configured

        from serial.tools import list_ports

        ports = list(list_ports.comports())
        candidates = [
            port.device
            for port in ports
            if "arduino" in (
                f"{port.description} {port.manufacturer or ''}"
            ).lower()
        ]
        if not candidates and len(ports) == 1:
            candidates = [ports[0].device]
        if not candidates:
            raise RuntimeError(
                "No Arduino load-cell controller found. Connect it or set "
                "ARDUINO_LOADCELL_PORT (for example COM5)."
            )
        return candidates[0]

    def _read_loop(self) -> None:
        parser = ArduinoProtocolParser()
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
            self._events.append(event)
        if self._handler is not None:
            self._handler(event)


ARDUINO_BRIDGE = ArduinoBridge()
