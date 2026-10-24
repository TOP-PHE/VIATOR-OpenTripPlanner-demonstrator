"""#407 — the bundled TLS intermediate for the AT NAP hosts.

No network: a local TLS server with a generated test PKI (root →
intermediate → leaf) that sends only its leaf, like
user.mobilitaetsverbuende.at.
"""

from __future__ import annotations

import asyncio
import http.server
import inspect
import re
import ssl
import threading
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.oid import NameOID

from app import tls_contexts

REPO = Path(__file__).resolve().parents[2]

# Pinned with the file (#407); a changed PEM must change this too.
BUNDLED_SHA256 = "973a41276ffd01e027a2aad49e34c37846d3e976ff6a620b6712e33832041aa6"
GODADDY_G2_INTERMEDIATE = "Go Daddy Secure Certificate Authority - G2"
GODADDY_G2_ROOT = "Go Daddy Root Certificate Authority - G2"


_PROXY_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def _no_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mounted transports read the proxy environment (`_env_proxy`): the
    local-server tests must not depend on the runner's proxy settings."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


# ───────────────────────────── test PKI ─────────────────────────────


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: str,
    issuer_key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
    dns: str | None = None,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if dns is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False
        ).add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


@dataclass(frozen=True)
class Pki:
    root_file: str  # the test trust store: the root alone
    other_root_file: str  # an unrelated root
    intermediate_pem: str
    leaf_chain_file: str  # leaf certificate + key, no intermediate


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    d = tmp_path_factory.mktemp("pki")
    root_key, inter_key, leaf_key, other_key = _key(), _key(), _key(), _key()
    root = _cert("ZZ Test Root", root_key, "ZZ Test Root", root_key, ca=True)
    other = _cert("YY Other Root", other_key, "YY Other Root", other_key, ca=True)
    inter = _cert("ZZ Test Intermediate", inter_key, "ZZ Test Root", root_key, ca=True)
    leaf = _cert(
        "localhost", leaf_key, "ZZ Test Intermediate", inter_key, ca=False, dns="localhost"
    )
    (d / "root.pem").write_text(_pem(root))
    (d / "other.pem").write_text(_pem(other))
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    (d / "leaf.pem").write_text(_pem(leaf) + key_pem)
    return Pki(str(d / "root.pem"), str(d / "other.pem"), _pem(inter), str(d / "leaf.pem"))


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: no request lines in the test output."""


@pytest.fixture(scope="module")
def leaf_only_server(pki: Pki) -> Iterator[str]:
    """https://localhost:<port>/ serving only the leaf (no intermediate)."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    server_ctx.load_cert_chain(pki.leaf_chain_file)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    httpd.socket = server_ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _default_context(pki: Pki) -> ssl.SSLContext:
    """What a client without the helper uses: the test root alone."""
    ctx = ssl.create_default_context(cafile=pki.root_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _get(url: str, *, verify: ssl.SSLContext, mounts: dict | None = None) -> int:
    async def run() -> int:
        async with httpx.AsyncClient(verify=verify, mounts=mounts, trust_env=False) as c:
            return (await c.get(url)).status_code

    return asyncio.run(run())


# ─────────────────────────── chain behaviour ───────────────────────────


def test_default_context_fails_on_a_leaf_only_server(pki: Pki, leaf_only_server: str) -> None:
    default = _default_context(pki)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _get(leaf_only_server, verify=default)


def test_helper_context_validates_a_leaf_only_server(pki: Pki, leaf_only_server: str) -> None:
    default = _default_context(pki)
    helper = tls_contexts.build_context(pki.intermediate_pem, cafile=pki.root_file)
    mounts = tls_contexts.mounts_for(frozenset({"localhost"}), helper)
    assert _get(leaf_only_server, verify=default, mounts=mounts) == 200


def test_host_outside_the_allow_list_keeps_the_default_context(
    pki: Pki, leaf_only_server: str
) -> None:
    default = _default_context(pki)
    helper = tls_contexts.build_context(pki.intermediate_pem, cafile=pki.root_file)
    mounts = tls_contexts.mounts_for(frozenset({"zz.example"}), helper)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _get(leaf_only_server, verify=default, mounts=mounts)


def test_the_intermediate_is_no_trust_anchor(pki: Pki, leaf_only_server: str) -> None:
    """Without the root in the store the chain does not verify: the bundled
    intermediate only fills the missing link (partial chains refused)."""
    helper = tls_contexts.build_context(pki.intermediate_pem, cafile=pki.other_root_file)
    assert not helper.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    mounts = tls_contexts.mounts_for(frozenset({"localhost"}), helper)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _get(leaf_only_server, verify=helper, mounts=mounts)


def test_helper_context_still_checks_the_host_name(pki: Pki, leaf_only_server: str) -> None:
    helper = tls_contexts.build_context(pki.intermediate_pem, cafile=pki.root_file)
    assert helper.check_hostname
    assert helper.verify_mode == ssl.CERT_REQUIRED
    assert helper.minimum_version >= ssl.TLSVersion.TLSv1_2
    by_ip = leaf_only_server.replace("localhost", "127.0.0.1")
    mounts = tls_contexts.mounts_for(frozenset({"127.0.0.1"}), helper)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _get(by_ip, verify=helper, mounts=mounts)


# ───────────────────────────── allow-list ─────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://user.mobilitaetsverbuende.at/auth/realms/x/protocol/openid-connect/token",
        "https://DATA.mobilitaetsverbuende.at/api/public/v1/data-sets/99/2099/file",
    ],
)
def test_context_for_an_allow_listed_host(url: str) -> None:
    ctx = tls_contexts.context_for(url)
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "url",
    [
        "https://zz.example/feed.zip",
        "https://evil.mobilitaetsverbuende.at.zz.example/",
        "https://mobilitaetsverbuende.at/",
        "http://user.mobilitaetsverbuende.at/",
    ],
)
def test_context_for_any_other_url_is_the_default(url: str) -> None:
    assert tls_contexts.context_for(url) is None


def test_tls_mounts_cover_exactly_the_allow_list() -> None:
    mounts = tls_contexts.tls_mounts()
    assert set(mounts) == {f"https://{h}" for h in tls_contexts.INTERMEDIATE_HOSTS}
    assert all(isinstance(t, httpx.AsyncHTTPTransport) for t in mounts.values())


def test_the_at_nap_hosts_in_use_are_allow_listed() -> None:
    """The token URL preset and the NAP download host (sources, preset, docs)."""
    text = "".join(
        (REPO / rel).read_text(encoding="utf-8")
        for rel in (
            "app/data/eu19_nap_sources.json",
            "app/templates/credentials.html",
            "docs/nap-feed-resolvers.md",
        )
    )
    used = set(re.findall(r"https://([a-z.]+\.mobilitaetsverbuende\.at)/", text))
    assert used
    assert used <= tls_contexts.INTERMEDIATE_HOSTS


def test_mounted_transport_follows_the_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.zz.example:3128")
    monkeypatch.setenv("NO_PROXY", "data.mobilitaetsverbuende.at")
    assert tls_contexts._env_proxy("https://user.mobilitaetsverbuende.at") == (
        "http://proxy.zz.example:3128"
    )
    assert tls_contexts._env_proxy("https://data.mobilitaetsverbuende.at") is None
    mounts = tls_contexts.tls_mounts()
    via_proxy = mounts["https://user.mobilitaetsverbuende.at"]
    direct = mounts["https://data.mobilitaetsverbuende.at"]
    assert isinstance(via_proxy, httpx.AsyncHTTPTransport)
    assert isinstance(direct, httpx.AsyncHTTPTransport)
    assert isinstance(via_proxy._pool, httpcore.AsyncHTTPProxy)
    assert not isinstance(direct._pool, httpcore.AsyncHTTPProxy)


def test_no_environment_proxy_means_a_direct_transport() -> None:
    assert tls_contexts._env_proxy("https://user.mobilitaetsverbuende.at") is None


def test_a_proxy_without_a_scheme_is_an_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """As httpx reads it: `p.zz.example:3128` must not raise in tls_mounts()."""
    monkeypatch.setenv("HTTPS_PROXY", "p.zz.example:3128")
    assert tls_contexts._env_proxy("https://user.mobilitaetsverbuende.at") == (
        "http://p.zz.example:3128"
    )
    transport = tls_contexts.tls_mounts()["https://user.mobilitaetsverbuende.at"]
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert isinstance(transport._pool, httpcore.AsyncHTTPProxy)


def test_production_context_trusts_certifis_root_and_knows_the_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context of the AT hosts, built as in production (httpx's default:
    certifi), holds the GoDaddy Root G2 and the bundled G2 intermediate."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    tls_contexts._bundled_context.cache_clear()
    try:
        ctx = tls_contexts.context_for("https://user.mobilitaetsverbuende.at/")
        assert ctx is not None
        common_names = {
            value
            for cert in ctx.get_ca_certs()
            for rdn in cert["subject"]
            for key, value in rdn
            if key == "commonName"
        }
    finally:
        tls_contexts._bundled_context.cache_clear()
    assert {GODADDY_G2_ROOT, GODADDY_G2_INTERMEDIATE} <= common_names
    assert len(common_names) > 100  # certifi's roots, not an empty store


# ─────────────────────────── the bundled PEM ───────────────────────────


def _bundled() -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(tls_contexts.EXTRA_INTERMEDIATES_PATH.read_bytes())


def test_bundled_pem_is_the_pinned_godaddy_g2_intermediate() -> None:
    (cert,) = _bundled()
    assert cert.fingerprint(hashes.SHA256()).hex() == BUNDLED_SHA256
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == GODADDY_G2_INTERMEDIATE
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert cert.not_valid_before_utc < datetime.now(UTC) < cert.not_valid_after_utc


def _certifi_roots() -> list[x509.Certificate]:
    """certifi's roots, parsed one by one: an old root with a serial number
    RFC 5280 disallows makes cryptography warn (and, in a later release,
    refuse); that one is skipped, it is not the root under test."""
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
        Path(certifi.where()).read_text(encoding="ascii"),
        flags=re.DOTALL,
    )
    roots = []
    for block in blocks:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CryptographyDeprecationWarning)
            try:
                roots.append(x509.load_pem_x509_certificate(block.encode("ascii")))
            except ValueError:
                continue
    return roots


def test_bundled_intermediate_chains_to_certifis_godaddy_root() -> None:
    (cert,) = _bundled()
    issuers = [r for r in _certifi_roots() if r.subject == cert.issuer]
    assert len(issuers) == 1
    root = issuers[0]
    assert root.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == GODADDY_G2_ROOT
    cert.verify_directly_issued_by(root)  # raises if the signature does not check


def test_bundled_pem_holds_certificates_only() -> None:
    text = tls_contexts.EXTRA_INTERMEDIATES_PATH.read_text(encoding="ascii")
    blocks = re.findall(r"-----BEGIN ([A-Z ]+)-----", text)
    assert blocks == ["CERTIFICATE"]
    # The header records the pinned fingerprint (provenance, #407).
    assert BUNDLED_SHA256 in text.replace(":", "").lower()


def test_expiry_warning_quiet_now_and_loud_near_the_end() -> None:
    assert tls_contexts.expiry_warning() is None
    (cert,) = _bundled()
    late = cert.not_valid_after_utc - timedelta(days=tls_contexts.EXPIRY_WARNING_DAYS - 1)
    message = tls_contexts.expiry_warning(late)
    assert message is not None
    assert message.startswith("tls.extra_intermediate_expiring")


def test_log_expiry_warning_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tls_contexts, "expiry_warning", lambda: "tls.zz_message")
    with caplog.at_level("WARNING", logger="app.tls_contexts"):
        tls_contexts.log_expiry_warning()
    assert [r.getMessage() for r in caplog.records] == ["tls.zz_message"]


def test_expiry_warning_names_an_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tls_contexts, "EXTRA_INTERMEDIATES_PATH", tmp_path / "missing.pem")
    assert tls_contexts.expiry_warning() == (
        "tls.extra_intermediates_unreadable type=FileNotFoundError"
    )


def test_web_checks_the_expiry_at_startup_and_daily() -> None:
    from app import main

    startup = inspect.getsource(main._startup)
    assert "tls_contexts.log_expiry_warning()" in startup
    assert 'tls_contexts.log_expiry_warning, "cron"' in startup


# ───────────────────────────── guard ─────────────────────────────

_UNVERIFIED = re.compile(
    r"verify\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False|_create_unverified_context"
)


def test_no_code_turns_tls_verification_off() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{n}"
        for top in ("app", "scripts", "bin", "alembic")
        for path in sorted((REPO / top).rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _UNVERIFIED.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("rel", "clients"),
    [
        ("app/api/credentials.py", 1),  # Test login
        ("app/api/admin/sessions.py", 2),  # refresh: login, NAP lookup, download
        ("app/master/nap_importer.py", 1),  # NAP catalogue import
    ],
)
def test_clients_of_the_at_nap_flow_use_the_helper(rel: str, clients: int) -> None:
    source = (REPO / rel).read_text(encoding="utf-8")
    assert source.count("httpx.AsyncClient(") == clients
    assert source.count("mounts=tls_contexts.tls_mounts()") == clients
