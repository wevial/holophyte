"""`run()` stops on a stop signal wherever the main thread is when it lands,
including inside socketserver's request dispatch, which catches `Exception`.

Run: python3 -m unittest discover -s tests -p 'test_serve_stop.py' -v
"""
from __future__ import annotations

import contextlib
import http.client
import io
import signal
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import holophyte.serve.server  # noqa: E402 - after the sys.path insert above
from holophyte.serve.serve_watch import CodeWatch  # noqa: E402 - after the insert
from tests.serve_fixture import ServeTestCase  # noqa: E402 - after the insert


class SignalledInDispatch(holophyte.serve.server.StatusServer):
    """SIGTERM reaches the main thread while it hands a request off."""

    def process_request(self, request, client_address):
        signal.raise_signal(signal.SIGTERM)
        super().process_request(request, client_address)


class StopSignalTests(ServeTestCase):
    def test_sigterm_during_request_dispatch_stops_the_daemon(self):
        self.seed()
        project = holophyte.config.project.Project.locate(self.target)
        server = SignalledInDispatch(project, ("127.0.0.1", 0))
        host, port = server.server_address[:2]
        out = io.StringIO()
        returned = threading.Event()
        kept_serving = []

        def request_then_wait():
            conn = http.client.HTTPConnection(host, port, timeout=10)
            with contextlib.suppress(OSError, http.client.HTTPException):
                conn.request("GET", "/status")
                conn.getresponse().read()
            conn.close()
            if not returned.wait(5):
                kept_serving.append(True)
                server.shutdown()

        client = threading.Thread(target=request_then_wait)
        client.start()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                code = holophyte.serve.server.run(
                    server, "under test", out, 60,
                    watch=CodeWatch(60, out, lambda: "aaa"))
        finally:
            returned.set()
            client.join()
        self.assertEqual(kept_serving, [], out.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("serve stopping on signal", out.getvalue())


if __name__ == "__main__":
    unittest.main()
