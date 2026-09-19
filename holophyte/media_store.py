"""S3-compatible evidence PUTs, signed with SigV4 using only the standard library."""
import hashlib
import hmac
import os
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

CREDENTIALS = ("HOLOPHYTE_MEDIA_ACCESS_KEY_ID", "HOLOPHYTE_MEDIA_SECRET_ACCESS_KEY")


def credentials():
    for name in CREDENTIALS:
        if not os.environ.get(name, "").strip():
            raise ValueError(f"media_bucket requires environment variable {name}")
    return tuple(os.environ[name] for name in CREDENTIALS)


def validate_bucket(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("media_bucket must be a table with endpoint, bucket, "
                         "public_base")
    if set(value) - {"endpoint", "bucket", "public_base", "retention_days"}:
        raise ValueError("media_bucket has unknown keys")
    for name in ("endpoint", "public_base"):
        url = value.get(name)
        if not isinstance(url, str):
            raise ValueError(f"media_bucket {name} must be an HTTP(S) URL")
        parsed = urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(ch.isspace() for ch in url)):
            raise ValueError(f"media_bucket {name} must be an HTTP(S) URL "
                             "without credentials or query")
    bucket = value.get("bucket")
    if not isinstance(bucket, str) or not bucket or any(
            ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in bucket):
        raise ValueError("media_bucket bucket must be an S3 bucket name")
    days = value.get("retention_days", 1)
    if type(days) is not int or days < 1:
        raise ValueError("media_bucket retention_days must be a positive integer")
    credentials()
    return dict(value)


def sign_put(url, payload, access_key, secret_key, timestamp, region="auto",
             headers=None):
    """Sign an already URI-encoded PUT URL (no query); return request headers."""
    parsed = urlsplit(url)
    signed = {key.lower(): " ".join(value.split())
              for key, value in (headers or {}).items()}
    signed.update(host=parsed.netloc,
                  **{"x-amz-date": timestamp,
                     "x-amz-content-sha256": hashlib.sha256(payload).hexdigest()})
    names = ";".join(sorted(signed))
    canonical_headers = "".join(f"{key}:{signed[key]}\n" for key in sorted(signed))
    canonical = "\n".join(("PUT", parsed.path or "/", "", canonical_headers,
                           names, signed["x-amz-content-sha256"]))
    scope = f"{timestamp[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(("AWS4-HMAC-SHA256", timestamp, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()))
    key = ("AWS4" + secret_key).encode()
    for part in (timestamp[:8], region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope},"
        f"SignedHeaders={names},Signature={signature}")
    return signed


def upload(config, key, file):
    access, secret = credentials()
    url = (config["endpoint"].rstrip("/") + "/" + quote(config["bucket"], safe="")
           + "/" + quote(key, safe="/"))
    payload = file.read_bytes()
    content_type = {".png": "image/png", ".webm": "video/webm",
                    ".mp4": "video/mp4"}[file.suffix]
    headers = sign_put(url, payload, access, secret,
                       datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                       headers={"content-type": content_type})
    request = Request(url, data=payload, headers=headers, method="PUT")
    with urlopen(request, timeout=60) as response:
        if not 200 <= response.status < 300:
            raise OSError("media bucket PUT failed")
    return config["public_base"].rstrip("/") + "/" + quote(key, safe="/")
