"""The X-Books-Signature contract (spec 0008 build plan task 3): golden vectors shared with frappe_books,
raw signed answers, and the `issued_at` window."""

import json
from datetime import timedelta
from pathlib import Path

from frappe.tests import UnitTestCase

from rarebooks_control import signing
from rarebooks_control.tests.control import issued

VECTORS = json.loads((Path(__file__).parent / "signing_vectors.json").read_text())["vectors"]


class UnitTestSigningVectors(UnitTestCase):
	def test_every_vector_signs_to_the_same_header(self):
		# covers: AC-2, AC-13 (contract vectors, the same file as frappe_books/tests/signing_vectors.json)
		for vector in VECTORS:
			with self.subTest(vector["name"]):
				body = vector["body"].encode()
				self.assertEqual(signing.sign(body, vector["secret"]), vector["signature"])
				self.assertTrue(signing.is_valid_signature(body, vector["signature"], vector["secret"]))

	def test_signed_response_sends_exactly_the_bytes_it_signed(self):
		# covers: AC-5 (one helper, raw signed bytes, the status set, no `message` wrapper)
		refusal = next(vector for vector in VECTORS if vector["name"] == "signed refusal")
		response = signing.signed_response(json.loads(refusal["body"]), 409, refusal["secret"])
		self.assertEqual(response.status_code, 409)
		self.assertEqual(response.get_data(), refusal["body"].encode())
		self.assertEqual(response.headers[signing.SIGNATURE_HEADER], refusal["signature"])

	def test_a_signature_with_non_ascii_characters_is_refused_not_raised(self):
		# covers: AC-5 (a forged header answers 401, never a 500 from compare_digest)
		for forged in ("sha256=\u00e9", "sha256=\u20ac" * 10, "sha256=\ud800"):
			with self.subTest(forged=forged):
				self.assertFalse(signing.is_valid_signature(b"{}", forged, "secret"))

	def test_a_401_carries_no_detail(self):
		# covers: AC-5
		response = signing.unauthorized("secret")
		self.assertEqual((response.status_code, response.get_data()), (401, b"{}"))
		self.assertNotIn(signing.SIGNATURE_HEADER, signing.unauthorized().headers)

	def test_issued_at_window(self):
		self.assertTrue(signing.is_fresh(issued()))
		self.assertTrue(signing.is_fresh(issued(timedelta(minutes=-4))))
		self.assertFalse(signing.is_fresh(issued(timedelta(minutes=-6))))
		self.assertFalse(signing.is_fresh(issued(timedelta(minutes=6))))
		# A time without a zone, or not a time at all, is never fresh.
		self.assertFalse(signing.is_fresh("2026-11-18T10:00:00"))
		self.assertFalse(signing.is_fresh(None))
