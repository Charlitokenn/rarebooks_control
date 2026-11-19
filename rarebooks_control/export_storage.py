"""The private R2 bucket that holds removal exports, through its S3 API (spec 0007 AC-7, AC-9, AC-11, AC-16).

The control site holds a read and delete token in `books_export_storage` of its `site_config.json`. Only this
module talks to R2, so tests patch its functions. The keys are never logged.
"""

import frappe

KEYS = ("endpoint_url", "bucket", "access_key_id", "secret_access_key")
# A download link opens a presigned URL valid this long (AC-7).
PRESIGNED_SECONDS = 60 * 60


class StorageError(Exception):
	"""R2 could not be asked, or `books_export_storage` is incomplete."""


def object_size(key: str) -> int | None:
	"""The object's size from an R2 HEAD request, or None when it doesn't exist (AC-9)."""
	from botocore.exceptions import ClientError

	client, bucket = _client()
	try:
		head = client.head_object(Bucket=bucket, Key=key)
	except ClientError as error:
		if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
			return None
		raise StorageError(f"R2 HEAD failed: {error.response.get('Error', {}).get('Code')}") from None
	return int(head["ContentLength"])


def delete_object(key: str):
	"""Delete the export (AC-11). Deleting a key that is already gone is not an error in S3."""
	from botocore.exceptions import ClientError

	client, bucket = _client()
	try:
		client.delete_object(Bucket=bucket, Key=key)
	except ClientError as error:
		raise StorageError(f"R2 delete failed: {error.response.get('Error', {}).get('Code')}") from None


def presigned_url(key: str, file_name: str) -> str:
	"""A GET URL for the export valid for an hour, saved under `file_name` (AC-7)."""
	client, bucket = _client()
	return client.generate_presigned_url(
		"get_object",
		Params={
			"Bucket": bucket,
			"Key": key,
			"ResponseContentDisposition": f'attachment; filename="{file_name}"',
		},
		ExpiresIn=PRESIGNED_SECONDS,
	)


def _client():
	import boto3
	from botocore.config import Config

	conf = frappe.conf.get("books_export_storage") or {}
	if not isinstance(conf, dict) or not all(isinstance(conf.get(key), str) and conf[key] for key in KEYS):
		raise StorageError("books_export_storage is missing or incomplete in site_config.json")
	client = boto3.client(
		"s3",
		endpoint_url=conf["endpoint_url"],
		aws_access_key_id=conf["access_key_id"],
		aws_secret_access_key=conf["secret_access_key"],
		region_name="auto",
		config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
	)
	return client, conf["bucket"]
