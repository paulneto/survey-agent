"""Upload (blob) store — where the bytes of a `file` question's answer live.

Separate seam from the session/response store: those hold JSON state, this holds binary blobs, and a
deployer wires them independently (`SURVEY_UPLOAD_STORE` vs `SURVEY_STORE`). The engine never touches
bytes — the web layer reads the multipart upload, calls `put(...)`, gets back a small **reference**
(`{backend, key, filename, size, content_type, url?}`), and stores that reference as the answer. The
export shape therefore carries only the reference, so DSAR / data-export tooling keeps working.

Backends (selected by SURVEY_UPLOAD_STORE):
  local   files under SURVEY_UPLOAD_DIR (default ./survey_uploads) — dev / single box.
  s3      AWS S3 via boto3 (the `[s3]` extra). SURVEY_UPLOAD_BUCKET + standard AWS creds/region from
          the environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION, or an instance role).
          `url()` returns a short-lived presigned GET URL.

Bring your own: set SURVEY_UPLOAD_STORE to an import path ("my_pkg.blobs:GCSUploadStore").

Interface (both backends implement):
  put(survey_id, response_id, question_id, filename, data, content_type=None) -> ref dict
  open(ref) -> bytes
  url(ref) -> str | None        # a (possibly presigned/served) URL, when the backend can produce one
  delete(ref) -> bool           # remove the bytes (erasure / a rejected or superseded upload)

Keys are unique per upload (a random component), so a retake or a re-upload under the same response
never overwrites earlier bytes — every stored ref keeps pointing at exactly what was uploaded.
"""
import os
import re
import secrets

BUILTINS = ("local", "s3")


def _safe_name(filename):
    """basename only, stripped of anything that isn't filename-safe."""
    base = os.path.basename(filename or "upload")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "upload"
    return base[:120]


def _key(survey_id, response_id, question_id, filename):
    parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", str(p)) for p in (survey_id, response_id, question_id)]
    # random component: two uploads with the same name under one response never collide/overwrite
    return "/".join(parts) + "/" + secrets.token_hex(8) + "_" + _safe_name(filename)


def _check_key(key):
    """A stored key is relative and never climbs out of the store root (refs come back from stored
    records, so treat them as untrusted when turning them into paths)."""
    key = str(key or "")
    norm = os.path.normpath(key)
    if not key or os.path.isabs(key) or norm.startswith("..") or "\0" in key:
        raise ValueError("invalid upload key")
    return norm


class LocalUploadStore:
    def __init__(self, root=None):
        self.root = root or os.environ.get("SURVEY_UPLOAD_DIR", "./survey_uploads")
        self.base_url = os.environ.get("SURVEY_UPLOAD_BASE_URL")  # optional public/served prefix

    def put(self, survey_id, response_id, question_id, filename, data, content_type=None):
        key = _key(survey_id, response_id, question_id, filename)
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        ref = {"backend": "local", "key": key, "filename": _safe_name(filename),
               "size": len(data), "content_type": content_type}
        if self.base_url:
            ref["url"] = self.base_url.rstrip("/") + "/" + key
        return ref

    def open(self, ref):
        with open(os.path.join(self.root, _check_key(ref["key"])), "rb") as f:
            return f.read()

    def url(self, ref):
        return ref.get("url")

    def delete(self, ref):
        """Remove the bytes for `ref`. Idempotent: True if something was removed, False if absent."""
        path = os.path.join(self.root, _check_key((ref or {}).get("key")))
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        # tidy now-empty per-question/per-response directories (never above root)
        d, root = os.path.dirname(path), os.path.abspath(self.root)
        while os.path.abspath(d).startswith(root + os.sep):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)
        return True


class S3UploadStore:
    def __init__(self, bucket=None, prefix=None, client=None):
        self.bucket = bucket or os.environ.get("SURVEY_UPLOAD_BUCKET")
        if not self.bucket:
            raise ValueError("S3UploadStore needs SURVEY_UPLOAD_BUCKET")
        self.prefix = (prefix if prefix is not None else os.environ.get("SURVEY_UPLOAD_PREFIX", "")).strip("/")
        self._client = client  # lazy; tests inject a fake

    @property
    def client(self):
        if self._client is None:
            import boto3  # the [s3] extra
            self._client = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
        return self._client

    def _full_key(self, key):
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, survey_id, response_id, question_id, filename, data, content_type=None):
        key = _key(survey_id, response_id, question_id, filename)
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=self._full_key(key), Body=data, **extra)
        return {"backend": "s3", "bucket": self.bucket, "key": key, "filename": _safe_name(filename),
                "size": len(data), "content_type": content_type}

    def open(self, ref):
        obj = self.client.get_object(Bucket=ref.get("bucket", self.bucket), Key=self._full_key(ref["key"]))
        return obj["Body"].read()

    def url(self, ref, expires=3600):
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": ref.get("bucket", self.bucket), "Key": self._full_key(ref["key"])},
            ExpiresIn=expires,
        )

    def delete(self, ref):
        """Remove the object for `ref` (S3 DeleteObject is idempotent). Returns True."""
        self.client.delete_object(Bucket=(ref or {}).get("bucket", self.bucket),
                                  Key=self._full_key(_check_key((ref or {}).get("key"))))
        return True


def _load_custom(path, **kwargs):
    import importlib
    mod, _, cls = path.replace(":", ".").rpartition(".")
    if not mod:
        raise ValueError(f"SURVEY_UPLOAD_STORE={path!r} is not an import path (use 'pkg.module:ClassName')")
    return getattr(importlib.import_module(mod), cls)(**kwargs)


def get_upload_store(kind=None, **kwargs):
    from survey_agent import env_select
    kind = (kind or env_select("SURVEY_UPLOAD_STORE", "local")) or "local"
    low = kind.lower()
    if low == "local":
        return LocalUploadStore(**kwargs)
    if low == "s3":
        return S3UploadStore(**kwargs)
    if "." in kind or ":" in kind:
        return _load_custom(kind, **kwargs)
    raise ValueError(
        f"unknown SURVEY_UPLOAD_STORE: {kind!r}. Use a built-in ({'|'.join(BUILTINS)}) or an import "
        f"path to your own upload store, e.g. 'my_pkg.blobs:GCSUploadStore'.")
