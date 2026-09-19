"""Independent SigV4 oracle and a real HTTP upload receiver."""
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from holophyte import media_store

CREDS = {
    "HOLOPHYTE_MEDIA_ACCESS_KEY_ID": "test-access-id",
    "HOLOPHYTE_MEDIA_SECRET_ACCESS_KEY": "test-secret-value",
}


@contextmanager
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            requests.append((self.command, self.path, dict(self.headers),
                             self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class SigningTests(unittest.TestCase):
    def test_aws_published_put_vector(self):
        # Literal oracle: "Example: PUT Object" at
        # https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html
        headers = media_store.sign_put(
            "https://examplebucket.s3.amazonaws.com/test%24file.text",
            b"Welcome to Amazon S3.", "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "20130524T000000Z", region="us-east-1",
            headers={"date": "Fri, 24 May 2013 00:00:00 GMT",
                     "x-amz-storage-class": "REDUCED_REDUNDANCY"})
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/"
            "us-east-1/s3/aws4_request,SignedHeaders=date;host;"
            "x-amz-content-sha256;x-amz-date;x-amz-storage-class,"
            "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")
