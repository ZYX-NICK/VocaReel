"""Small, dependency-free mpv JSON IPC client (Windows named pipe / Unix socket)."""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, BinaryIO


PROPERTY_NAMES = (
    "track-list",
    "sub-text",
    "secondary-sub-text",
    "time-pos",
    "duration",
    "pause",
    "path",
    "secondary-sub-visibility",
)


class MpvClient:
    """Every callback runs on a background thread; the UI must queue its events."""

    def __init__(self, callback: Callable[[dict[str, Any]], None]):
        self.callback = callback
        self.process: subprocess.Popen[bytes] | None = None
        self.endpoint: str | None = None
        self.stream: BinaryIO | None = None
        self.sock: socket.socket | None = None
        self.outgoing: queue.Queue[bytes | None] = queue.Queue()
        self.ready = False
        self.stopping = threading.Event()

    def start(self, mpv_executable: str) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
        if self.sock is not None:
            self.sock.close()
        self.stream = None
        self.sock = None
        self.ready = False
        self.outgoing = queue.Queue()
        self.stopping.clear()
        unique = "series_english_" + uuid.uuid4().hex[:12]
        self.endpoint = (
            r"\\.\pipe" + chr(92) + unique if os.name == "nt"
            else os.path.join(tempfile.gettempdir(), unique + ".sock")
        )
        args = [
            mpv_executable,
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            "--no-terminal",
            "--sub-auto=no",
            "--input-ipc-server=" + self.endpoint,
        ]
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        threading.Thread(target=self._connect_and_read, daemon=True).start()

    def _connect_and_read(self) -> None:
        try:
            deadline = time.monotonic() + 10
            while not self.stopping.is_set():
                if self.process is None or self.process.poll() is not None:
                    raise RuntimeError("mpv 未能启动，请检查 mpv 路径")
                try:
                    if os.name == "nt":
                        self.stream = open(self.endpoint, "r+b", buffering=0)  # type: ignore[arg-type]
                    else:
                        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        self.sock.connect(self.endpoint)  # type: ignore[arg-type]
                        self.stream = self.sock.makefile("rwb", buffering=0)
                    break
                except (FileNotFoundError, ConnectionRefusedError, OSError):
                    if self.sock:
                        self.sock.close()
                        self.sock = None
                    if time.monotonic() >= deadline:
                        raise RuntimeError("无法连接 mpv 通信接口") from None
                    time.sleep(0.1)

            if self.stopping.is_set():
                return
            self.ready = True
            threading.Thread(target=self._write_loop, daemon=True).start()
            for number, name in enumerate(PROPERTY_NAMES, start=1):
                self.send("observe_property", number, name)
            self.callback({"event": "connected"})
            assert self.stream is not None
            while not self.stopping.is_set():
                line = self.stream.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict):
                    self.callback(event)
        except Exception as exc:
            if not self.stopping.is_set():
                self.callback({"event": "client-error", "message": str(exc)})
        finally:
            self.ready = False
            if not self.stopping.is_set():
                self.callback({"event": "disconnected"})

    def _write_loop(self) -> None:
        """Only this worker writes to mpv; UI callbacks only enqueue commands."""
        while True:
            payload = self.outgoing.get()
            if payload is None:
                break
            try:
                assert self.stream is not None
                self.stream.write(payload)
                self.stream.flush()
            except (OSError, ValueError, AssertionError) as exc:
                self.ready = False
                if not self.stopping.is_set():
                    self.callback({"event": "client-error", "message": f"播放器通信中断：{exc}"})
                break

    def send(self, command: str, *args: Any) -> bool:
        payload = (json.dumps({"command": [command, *args]}, ensure_ascii=False) + "\n").encode()
        if not self.ready or self.stopping.is_set():
            return False
        self.outgoing.put_nowait(payload)
        return True

    def stop(self) -> None:
        self.send("quit")
        self.stopping.set()
        self.outgoing.put_nowait(None)
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
        if self.sock is not None:
            self.sock.close()
        self.ready = False
        if self.endpoint and os.name != "nt":
            try:
                os.unlink(self.endpoint)
            except FileNotFoundError:
                pass
