"""
daq/ivmux.py

The bench's IV MUX: iv_mux.MuxController limited to the channels actually
fitted, plus a record of every line on its serial port (the web UI's serial
monitor reads TRAFFIC).

The firmware addresses 6 boards x 15 channels (1-90) and always dumps six
words; only the first boards are fitted, so n_channels comes from
config.ivmux_channels and anything above it is refused before it reaches the
firmware -- a relay on an absent board "succeeds" with nothing connected.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque

from iv_mux import MuxController
from iv_mux.driver import RESPONSE_ERR, RESPONSE_OK

# (seq, unix time, "tx" | "rx" | "info", text, tag). tag is "poll" for the
# UI's periodic state reads, "user" for raw monitor commands, "" otherwise.
TRAFFIC: deque = deque(maxlen=2000)
_SEQ = itertools.count(1)
_tag = threading.local()

_RAW_TIMEOUT_S = 3.0


def _record(direction: str, text: str) -> None:
    TRAFFIC.append((next(_SEQ), time.time(), direction, text,
                    getattr(_tag, "value", "")))


class _SerialTap:
    """Pass-through for the driver's pyserial port that records each line.

    The driver does its I/O in the calling thread under its own lock, so the
    thread-local tag set by the caller labels both the command and its reply.
    """

    def __init__(self, ser):
        self._ser = ser

    def write(self, data: bytes):
        _record("tx", data.decode(errors="replace").strip() or repr(data))
        return self._ser.write(data)

    def readline(self) -> bytes:
        raw = self._ser.readline()
        if raw:
            _record("rx", raw.decode(errors="replace").rstrip("\r\n"))
        return raw

    def close(self) -> None:
        _record("info", "port closed")
        self._ser.close()

    def __getattr__(self, name):
        return getattr(self._ser, name)


class IVMux(MuxController):

    def __init__(self, port: str, n_channels: int, mode: str = "hardware"):
        super().__init__(port=port, mode=mode)
        self.n_channels = n_channels
        self.port = port

    def connect(self):
        super().connect()
        if self._drv._ser is not None:
            self._drv._ser = _SerialTap(self._drv._ser)
            _record("info", f"port open: {self.port} @ 9600 8N1")

    def _check(self, channel) -> None:
        if not 1 <= int(channel) <= self.n_channels:
            raise ValueError(f"IV MUX channel {channel} is outside the fitted "
                             f"range 1-{self.n_channels}")

    def select(self, channel: int, settle_s: float = 0.0):
        self._check(channel)
        super().select(channel, settle_s)

    def sweep(self, channels, *args, **kwargs):
        for ch in channels:
            self._check(ch)
        return super().sweep(channels, *args, **kwargs)

    def sequence(self, start: int, stop: int, *args, **kwargs):
        self._check(start)
        self._check(stop)
        return super().sequence(start, stop, *args, **kwargs)

    def poll_active_channel(self) -> int | None:
        """active_channel() for periodic UI polls, tagged so the serial
        monitor can hide them."""
        _tag.value = "poll"
        try:
            return self.active_channel()
        finally:
            _tag.value = ""

    def send_raw(self, line: str) -> list[str]:
        """Send one firmware command line as typed; return the lines received
        up to 'Command complete' / 'Error in command', or whatever arrived
        before the timeout."""
        line = line.strip()
        parts = line.split()
        cmd = parts[0] if parts else ""
        timeout_s = _RAW_TIMEOUT_S
        try:
            if cmd in ("a", "b", "s") and len(parts) >= 2:
                self._check(parts[1])
            elif cmd == "q" and len(parts) >= 4:
                self._check(parts[1])
                self._check(parts[2])
                n = abs(int(parts[2]) - int(parts[1])) + 1
                timeout_s = max(timeout_s, n * (abs(int(parts[3])) / 1000 + 0.5) + 5)
        except ValueError as e:
            if "fitted range" in str(e):
                raise
            # Malformed numbers: let the firmware answer "Error in command".

        drv = self._drv
        ser = drv._ser
        real = getattr(ser, "_ser", ser)
        _tag.value = "user"
        try:
            with drv._lock:
                old_timeout = real.timeout
                real.timeout = 0.2      # the driver's 5 s would overrun the window
                try:
                    ser.reset_input_buffer()
                    ser.write((line + "\n").encode())
                    lines = []
                    deadline = time.monotonic() + timeout_s
                    while time.monotonic() < deadline:
                        raw = ser.readline()
                        if not raw:
                            continue
                        text = raw.decode(errors="replace").strip()
                        lines.append(text)
                        if RESPONSE_OK in text or RESPONSE_ERR in text:
                            break
                    if cmd == "t":
                        # 't' streams temperatures until any character arrives.
                        ser.write(b"x")
                        end = time.monotonic() + 5.0
                        while time.monotonic() < end:
                            raw = ser.readline()
                            if raw and RESPONSE_OK in raw.decode(errors="replace"):
                                break
                    return lines
                finally:
                    real.timeout = old_timeout
        finally:
            _tag.value = ""
