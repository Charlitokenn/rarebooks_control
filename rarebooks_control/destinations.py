"""Push policy: HTTPS on port 443, public unicast addresses only, no redirects or proxies."""

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool


def resolve_destination(url: str) -> tuple[str, str]:
	try:
		parts = urlsplit(url)
		if (
			parts.scheme != "https"
			or not parts.hostname
			or parts.username is not None
			or parts.password is not None
			or parts.port not in (None, 443)
			or parts.fragment
		):
			raise ValueError("invalid push destination")
		addresses = {
			result[4][0] for result in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
		}
		if not addresses or any(not allowed_address(address) for address in addresses):
			raise ValueError("nonpublic push destination")
		return parts.hostname, sorted(addresses)[0]
	except (ValueError, TypeError, OSError) as error:
		raise ValueError("invalid push destination") from error


def allowed_address(address: str) -> bool:
	ip = ipaddress.ip_address(address)
	return (
		ip.is_global
		and not ip.is_multicast
		and not ip.is_unspecified
		and not getattr(ip, "ipv4_mapped", None)
	)


def public_destination(url) -> bool:
	if not isinstance(url, str):
		return False
	try:
		resolve_destination(url)
		return True
	except ValueError:
		return False


class PinnedAdapter(HTTPAdapter):
	def __init__(self, hostname: str, address: str):
		super().__init__()
		# Connect to the checked numeric address, while verifying and sending SNI for the hostname.
		self.pool = HTTPSConnectionPool(address, port=443, assert_hostname=hostname, server_hostname=hostname)

	def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
		return self.pool

	def close(self):
		self.pool.close()
		super().close()


def post(url: str, **kwargs):
	hostname, address = resolve_destination(url)
	with requests.Session() as session:
		session.trust_env = False
		session.mount("https://", PinnedAdapter(hostname, address))
		kwargs["headers"] = {**kwargs.get("headers", {}), "Host": hostname}
		kwargs["allow_redirects"] = False
		return session.post(url, **kwargs)
