"""Fake upload destination: records what the app POSTs, and can be told to fail.

Stands in for the platform server (hw_trigger.destination_url). It keeps every upload in memory with its form fields
and image bytes so a test can inspect them, and `fail_next` makes the next N uploads return HTTP 500 so the app's
retry / save-locally behaviour can be exercised.

Runs on a background thread: `srv = FakeUploadServer().start(); srv.url; srv.uploads; srv.stop()`.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from flask import Flask, jsonify, request
from werkzeug.serving import make_server


@dataclass
class Upload:
    filename: str
    image: bytes
    fields: dict = field(default_factory=dict)


class FakeUploadServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.uploads: list[Upload] = []
        self.attempts = 0  # every POST seen, including ones answered with a failure
        self.fail_next = 0
        self._lock = threading.Lock()

        logging.getLogger("werkzeug").setLevel(logging.ERROR)  # one access-log line per upload drowns the test output
        app = Flask(__name__)

        @app.post("/upload")
        def upload():
            with self._lock:
                self.attempts += 1
                if self.fail_next > 0:
                    self.fail_next -= 1
                    return jsonify({"error": "injected failure"}), 500
                img = request.files.get("image")
                self.uploads.append(Upload(
                    filename=img.filename if img else "",
                    image=img.read() if img else b"",
                    fields=dict(request.form),
                ))
            return jsonify({"ok": True})

        @app.get("/health")
        def health():
            return jsonify({"ok": True})

        self._server = make_server(host, port, app, threaded=True)
        self.host = host
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="fake-upload-server")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/upload"

    def start(self) -> "FakeUploadServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=3)

    def snapshot(self) -> list[Upload]:
        with self._lock:
            return list(self.uploads)
