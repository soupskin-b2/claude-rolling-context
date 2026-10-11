"""
Shared MITM front-end for the body-rewriting Claude Code proxies
(rolling-context, pii-proxy).

Why this exists
---------------
These plugins rewrite request/response bodies. Historically they took the
``ANTHROPIC_BASE_URL`` slot so Claude Code would POST to them in plaintext.
But a non-anthropic ``ANTHROPIC_BASE_URL`` host trips Claude Code's Remote
Control / GrowthBook gate. Routing through ``HTTPS_PROXY`` instead leaves the
destination as ``api.anthropic.com`` (gate untouched) — but then the traffic
is TLS end-to-end and the body is opaque.

This module bridges the gap: it terminates TLS for ``api.anthropic.com`` with a
locally generated CA (trusted by Claude Code via ``NODE_EXTRA_CA_CERTS``), then
hands the decrypted, plaintext HTTP connection to the plugin's *existing*
``BaseHTTPRequestHandler`` — so all body-rewriting, streaming and chaining code
is reused untouched. Every other CONNECT host is blind-tunnelled unchanged.

The same file ships verbatim in both plugins.
"""
from __future__ import annotations

import datetime
import os
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

# Hosts intercepted by default; everything else is blind-tunnelled so telemetry,
# updates and any unrelated TLS pass through untouched. A non-Anthropic upstream
# (a custom gateway configured via ANTHROPIC_BASE_URL) is added at runtime so it
# is intercepted too — see ensure_ca(extra_hosts=...) and serve(hosts=...).
MITM_HOSTS = {"api.anthropic.com"}

_CA_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# CA + leaf generation
# --------------------------------------------------------------------------
def ensure_ca(ca_dir: str, extra_hosts=()):
    """Generate (once) a local CA and a leaf cert covering the MITM hosts.

    Returns (ca_cert_path, server_ssl_context, hosts). `hosts` is the full set
    of intercepted hostnames (defaults plus extra_hosts). The CA cert is what
    the caller advertises to Claude Code via NODE_EXTRA_CA_CERTS. Idempotent and
    process-safe: the CA is generated once; the leaf is regenerated only when it
    does not yet cover every requested host.
    """
    os.makedirs(ca_dir, exist_ok=True)
    ca_cert_path = os.path.join(ca_dir, "ca-cert.pem")
    ca_key_path = os.path.join(ca_dir, "ca-key.pem")
    leaf_path = os.path.join(ca_dir, "leaf.pem")  # cert + key, PEM bundle
    hosts_path = os.path.join(ca_dir, "leaf-hosts.txt")

    hosts = set(MITM_HOSTS) | {h.strip().lower() for h in extra_hosts if h and h.strip()}

    with _CA_LOCK:
        have_ca = os.path.exists(ca_cert_path) and os.path.exists(ca_key_path)
        covered = set()
        if os.path.exists(hosts_path):
            try:
                with open(hosts_path, encoding="utf-8") as f:
                    covered = {ln.strip().lower() for ln in f if ln.strip()}
            except OSError:
                covered = set()
        if (not have_ca or not os.path.exists(leaf_path) or not hosts <= covered
                or not _has_key_ids(ca_cert_path, leaf_path)):
            _generate(ca_cert_path, ca_key_path, leaf_path, hosts_path, sorted(hosts),
                      reuse_ca=have_ca)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf_path)
    return ca_cert_path, ctx, hosts


def _has_key_ids(ca_cert_path: str, leaf_path: str) -> bool:
    """Whether the CA has a Subject Key Identifier and the leaf an Authority
    Key Identifier. Certificates made before these were added fail Python
    3.13+'s strict verification; they are re-issued (same CA key) when not."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID

        def has(path, oid):
            with open(path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            return any(e.oid == oid for e in cert.extensions)

        return (has(ca_cert_path, ExtensionOID.SUBJECT_KEY_IDENTIFIER)
                and has(leaf_path, ExtensionOID.AUTHORITY_KEY_IDENTIFIER))
    except Exception:
        return True   # unreadable: leave it to the load below to report


def _generate(ca_cert_path: str, ca_key_path: str, leaf_path: str,
              hosts_path: str, hosts, reuse_ca: bool = False) -> None:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    def _atomic_write(path: str, data: bytes) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Claude Local Proxy CA")])

    reissue_ca = False
    if reuse_ca:
        # Only the leaf needs to change (new host added) — keep the CA key so
        # the CA already trusted via NODE_EXTRA_CA_CERTS stays valid.
        with open(ca_key_path, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        # A CA made before the Subject Key Identifier was added: re-issue it
        # with the same key and name. Sessions that loaded the old copy still
        # verify leaves against it; new ones get one strict mode accepts.
        with open(ca_cert_path, "rb") as f:
            old_ca = x509.load_pem_x509_certificate(f.read())
        reissue_ca = not any(e.oid == x509.oid.ExtensionOID.SUBJECT_KEY_IDENTIFIER
                             for e in old_ca.extensions)
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if not reuse_ca or reissue_ca:
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name).issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                         crl_sign=True, key_encipherment=False,
                                         content_commitment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            # Python 3.13+ verifies in strict X.509 mode by default, which
            # rejects a CA without a Subject Key Identifier.
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _atomic_write(ca_cert_path, ca_cert.public_bytes(serialization.Encoding.PEM))
        _atomic_write(ca_key_path, ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [x509.DNSName(h) for h in hosts]
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # Strict mode (Python 3.13+) also wants these on the leaf: without the
        # Authority Key Identifier it fails "Missing Authority Key Identifier".
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _atomic_write(leaf_path,
                  leaf_cert.public_bytes(serialization.Encoding.PEM)
                  + leaf_key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    _atomic_write(hosts_path, ("\n".join(hosts) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# CONNECT proxy
# --------------------------------------------------------------------------
def _read_head(sock: socket.socket):
    """Read a request line + headers: ``(bytes read, None)`` or ``(None, reason)``.

    The bytes may run past the blank line (a body the client sent at once).
    """
    tls_hello = ("client spoke TLS directly to the proxy port "
                 "(HTTPS_PROXY must be an http:// URL, not https://)")
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            if not buf:
                return None, "client closed before sending any request"
            if buf[:1] == b"\x16":
                return None, tls_hello
            return None, ("client closed mid-request after "
                          f"{len(buf)} bytes, no header terminator")
        buf += chunk
        # A TLS ClientHello (HTTPS_PROXY set to an https:// URL, or a client that
        # speaks TLS to the proxy) starts with the 0x16 handshake record byte and
        # never carries a CRLF header terminator — catch it before we block for
        # one. It is the common misconfiguration; say so instead of timing out.
        if buf[:1] == b"\x16":
            return None, tls_hello
        if len(buf) > 65536:
            return None, "request headers exceeded 64 KiB without a CONNECT"
    return buf, None


def _read_connect_line(sock: socket.socket):
    """Read the CONNECT request line + headers.

    Returns ``(host, port)`` on success, or ``(None, reason)`` — a short human
    reason the front-end can log — so a connection we close before a usable
    CONNECT is never dropped silently (issue #12: a running session flipped onto
    a transport it could not satisfy hit this path and the proxy logged nothing).
    """
    buf, reason = _read_head(sock)
    if buf is None:
        return None, reason
    return _parse_connect(buf)


def _parse_connect(buf: bytes):
    first = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = first.split(" ")
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None, f"first line was not a CONNECT: {first[:80]!r}"
    hostport = parts[1]
    host, _, port = hostport.partition(":")
    return host, int(port or "443")


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _plain_local(buf: bytes, local_ports):
    """``(port, head)`` for a plain-HTTP request to one of our own ports.

    A client that sends every request through HTTPS_PROXY - Claude Code's own
    plugin fetch does, http:// included - asks this port for the proxy's
    /health as ``GET http://127.0.0.1:5588/health``, not as a CONNECT. Relayed
    to that port with the request line in origin form; anything else - another
    host, another port - is still refused, so this is no open proxy.
    """
    first, sep, rest = buf.partition(b"\r\n")
    parts = first.split(b" ")
    if len(parts) != 3 or not parts[1].lower().startswith(b"http://"):
        return None
    u = urlsplit(parts[1].decode("latin1", "replace"))
    try:
        port = u.port
    except ValueError:
        return None
    if (u.hostname or "").lower() not in _LOOPBACK or port not in local_ports:
        return None
    target = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return port, b" ".join([parts[0], target.encode("latin1"), parts[2]]) + sep + rest


def _relay_local(client: socket.socket, port: int, head: bytes) -> None:
    try:
        upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
        upstream.sendall(head)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        return
    _pipe(client, upstream)


def _blind_tunnel(client: socket.socket, host: str, port: int) -> None:
    try:
        upstream = socket.create_connection((host, port), timeout=30)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        return
    _pipe(client, upstream)


def _pipe(client: socket.socket, upstream: socket.socket) -> None:
    socks = [client, upstream]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 300)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (upstream if s is client else client).sendall(data)
    except OSError:
        pass
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def bind_listener(host: str, port: int) -> socket.socket:
    """A listening socket that no other process can share.

    On Windows SO_REUSEADDR does not mean "reuse a TIME_WAIT address" as it
    does on POSIX — it lets a second process bind a port that is already
    listening. Two proxies then split the connections between them, and one
    that was started with a different CA (a test run's throwaway home) answers
    TLS handshakes Claude Code cannot verify. SO_EXCLUSIVEADDRUSE makes that
    second bind fail instead.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
        srv.listen(128)
    except OSError:
        srv.close()
        raise
    return srv


def serve(mitm_port: int, ca_dir: str, on_terminated, log=None, host="127.0.0.1",
          extra_hosts=(), listener=None, local_ports=()):
    """Start the CONNECT front-end. Blocks; run in a thread.

    on_terminated(tls_sock, client_addr, sni_host) is called with a decrypted,
    server-side TLS socket for each intercepted connection. It owns the socket.
    extra_hosts adds non-default hostnames to intercept (e.g. a custom gateway).
    local_ports are this proxy's own loopback ports, which a plain-HTTP
    request through this front-end is relayed to (see _plain_local).
    """
    try:
        ca_cert_path, ctx, hosts = ensure_ca(ca_dir, extra_hosts)
    except ImportError:
        if listener is not None:
            listener.close()      # never hold a port open that never accepts
        if log:
            log("MITM front-end NOT started: the 'cryptography' package is missing, "
                "so the local CA cannot be generated. Install it in this proxy's "
                "environment (it is in requirements.txt). HTTPS_PROXY entrypoint is disabled.")
        return
    except Exception as e:  # noqa: BLE001
        if listener is not None:
            listener.close()
        if log:
            log(f"MITM front-end NOT started: CA setup failed ({e!r}). "
                "HTTPS_PROXY entrypoint is disabled.")
        return
    if log:
        log(f"MITM front-end: CA at {ca_cert_path}, intercepting {sorted(hosts)}")

    global _listener
    srv = listener or bind_listener(host, mitm_port)
    _listener = srv
    if log:
        log(f"MITM front-end listening on {host}:{mitm_port}")

    def handle(client, addr):
        peer = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) and len(addr) >= 2 else str(addr)
        try:
            buf, reason = _read_head(client)
            if buf is not None:
                local = _plain_local(buf, local_ports)
                if local is not None:
                    _relay_local(client, *local)
                    return
                thost, tport = _parse_connect(buf)
            else:
                thost, tport = None, reason
            if thost is None:
                # tport carries the reason string. Never close silently — a
                # stranded session (issue #12) reaches exactly here.
                if log:
                    log(f"MITM: closed {peer} before a usable CONNECT - {tport}")
                client.close()
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if thost.lower() in hosts:
                try:
                    tls = ctx.wrap_socket(client, server_side=True)
                except ssl.SSLError as e:
                    if log:
                        hint = ""
                        if "UNKNOWN_CA" in str(e) or "unknown ca" in str(e).lower():
                            hint = (" - the client does not trust our MITM CA; "
                                    "it was almost certainly started before "
                                    "NODE_EXTRA_CA_CERTS was wired in, so restart "
                                    "or --resume that session")
                        log(f"TLS handshake failed for {thost} from {peer}: {e}{hint}")
                    client.close()
                    return
                on_terminated(tls, addr, thost)
            else:
                _blind_tunnel(client, thost, tport)
        except Exception as e:  # noqa: BLE001 - never let one conn kill the loop
            if log:
                log(f"MITM conn error: {e!r}")
            try:
                client.close()
            except OSError:
                pass

    while True:
        try:
            client, addr = srv.accept()
        except OSError:
            # Listener closed by close_listener() during a drain.
            return
        threading.Thread(target=handle, args=(client, addr), daemon=True).start()


_listener = None


def close_listener():
    """Stop accepting CONNECTs (drain). Established tunnels are untouched."""
    global _listener
    srv, _listener = _listener, None
    if srv is not None:
        # On Linux, close() alone does not wake the thread blocked in
        # accept(), and the port stays bound (and accepting) until it does:
        # a successor's bind fails. shutdown() wakes it and lets go of the port.
        try:
            srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            srv.close()
        except OSError:
            pass


def start_in_thread(mitm_port: int, ca_dir: str, on_terminated, log=None,
                    extra_hosts=(), local_ports=()) -> threading.Thread:
    # Bind here, in the caller, so a port that is already taken raises where
    # it can be acted on instead of dying silently inside the thread.
    # A successor (started right after asking the running proxy to hand
    # over) may find the port still held for a moment while it lets go.
    bind_until = time.time() + (10 if os.environ.get("ROLLING_CONTEXT_SUCCESSOR") else 0)
    while True:
        try:
            listener = bind_listener("127.0.0.1", mitm_port)
            break
        except OSError:
            if time.time() >= bind_until:
                raise
            time.sleep(0.2)
    t = threading.Thread(target=serve,
                         args=(mitm_port, ca_dir, on_terminated, log, "127.0.0.1", extra_hosts,
                               listener, tuple(local_ports)),
                         daemon=True)
    t.start()
    return t
