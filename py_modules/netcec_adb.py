"""A small ADB client over the network (TCP port 5555): connect, log in with an RSA key, run shell commands.

NetCEC uses it for Android TVs that have no control API of their own (TCL and others). It is written without
third-party libraries because Decky's Python is trimmed down and cannot install any.
Protocol: https://android.googlesource.com/platform/system/core/+/master/adb/protocol.txt
"""
import base64
import json
import os
import socket
import struct
import time

A_CNXN, A_AUTH, A_OPEN, A_OKAY, A_CLSE, A_WRTE = (struct.unpack("<I", c)[0] for c in
                                                  (b"CNXN", b"AUTH", b"OPEN", b"OKAY", b"CLSE", b"WRTE"))
AUTH_TOKEN, AUTH_SIGNATURE, AUTH_RSAPUBLICKEY = 1, 2, 3
A_VERSION = 0x01000001  # protocol 1.0.1: the receiver does not check the checksum (it is sent anyway)
MAX_DATA = 256 * 1024
KEY_BITS = 2048
# PKCS#1 v1.5 DigestInfo prefix of SHA-1: adb signs the 20-byte token as if it were a SHA-1 hash
_SHA1_PREFIX = bytes.fromhex("3021300906052b0e03021a05000414")
_SMALL_PRIMES = [p for p in range(3, 1000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


class AdbError(Exception):
    pass


class NeedsApproval(AdbError):
    """The TV shows "Allow debugging?" and nobody has answered yet."""


# ---------- the RSA key (made once, kept in a private file)

def _is_probable_prime(n, rounds=24):
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n == p:
            return True
        if n % p == 0:
            return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = 2 + int.from_bytes(os.urandom(n.bit_length() // 8 + 1), "big") % (n - 3)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits):
    while True:
        # top two bits set: the product of two such primes has exactly 2 * bits bits; odd
        candidate = int.from_bytes(os.urandom(bits // 8), "big") | (3 << (bits - 2)) | 1
        if _is_probable_prime(candidate):
            return candidate


def generate_key(bits=KEY_BITS):
    e = 65537
    while True:
        p, q = _random_prime(bits // 2), _random_prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e != 0:
            return {"n": p * q, "e": e, "d": pow(e, -1, phi)}


def load_or_create_key(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: int(data[k], 16) for k in ("n", "e", "d")}
    except (OSError, ValueError, KeyError):
        pass
    key = generate_key()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the private key: owner only
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({k: format(v, "x") for k, v in key.items()}, f)
    os.replace(tmp, path)
    return key


def public_key_blob(key, name="netcec@steamos"):
    """The public key in the format adb sends to the TV (base64 of Android's RSAPublicKey struct, then the name)."""
    n, e = key["n"], key["e"]
    words = KEY_BITS // 32
    n0inv = (-pow(n & 0xFFFFFFFF, -1, 1 << 32)) & 0xFFFFFFFF
    rr = pow(1 << KEY_BITS, 2, n)
    raw = (struct.pack("<II", words, n0inv) + n.to_bytes(KEY_BITS // 8, "little")
           + rr.to_bytes(KEY_BITS // 8, "little") + struct.pack("<I", e))
    return base64.b64encode(raw) + b" " + name.encode() + b"\0"


def sign_token(key, token):
    size = (key["n"].bit_length() + 7) // 8
    padding = b"\xff" * (size - 3 - len(_SHA1_PREFIX) - len(token))
    message = b"\x00\x01" + padding + b"\x00" + _SHA1_PREFIX + token
    return pow(int.from_bytes(message, "big"), key["d"], key["n"]).to_bytes(size, "big")


# ---------- the connection

def _pack(command, arg0, arg1, data=b""):
    header = struct.pack("<6I", command, arg0, arg1, len(data), sum(data) & 0xFFFFFFFF, command ^ 0xFFFFFFFF)
    return header + data


class Connection:
    def __init__(self, host, key, port=5555, timeout=4.0):
        self.host, self.port, self.key, self.timeout = host, port, key, timeout
        self.sock = None
        self.max_data = 4096
        self._next_id = 0

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    @property
    def alive(self):
        return self.sock is not None

    def _send(self, command, arg0, arg1, data=b"", deadline=None):
        try:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout("timed out")
                self.sock.settimeout(left)
            self.sock.sendall(_pack(command, arg0, arg1, data))
        except socket.timeout:
            self.close()
            raise
        except OSError as e:
            self.close()
            raise AdbError(f"send failed: {e}")

    def _read(self, size, deadline):
        buf = bytearray()
        while len(buf) < size:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("timed out")
            self.sock.settimeout(left)
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                self.close()
                raise AdbError("connection closed by the TV")
            buf.extend(chunk)
        return bytes(buf)

    def _recv(self, deadline):
        command, arg0, arg1, length, _check, magic = struct.unpack("<6I", self._read(24, deadline))
        if magic != command ^ 0xFFFFFFFF:
            self.close()
            raise AdbError("not an adb answer")
        if length > self.max_data:
            self.close()
            raise AdbError(f"ADB packet is too large ({length} bytes)")
        return command, arg0, arg1, self._read(length, deadline) if length else b""

    def connect(self, approval_wait=0.0):
        """Log in. If the TV does not know our key yet it asks its user; we wait up to approval_wait seconds
        (0: do not wait) and raise NeedsApproval if nobody has answered."""
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise AdbError(f"no connection: {e}")
        deadline = time.monotonic() + self.timeout
        signed = offered = False
        try:
            self._send(A_CNXN, A_VERSION, MAX_DATA, b"host::\0", deadline)
            while True:
                command, arg0, _arg1, data = self._recv(deadline)
                if command == A_CNXN:
                    self.max_data = min(_arg1 or 4096, MAX_DATA)  # the TV tells how much data it takes at once
                    return self
                if command != A_AUTH or arg0 != AUTH_TOKEN:
                    raise AdbError("unexpected answer while logging in")
                if not signed:
                    self._send(A_AUTH, AUTH_SIGNATURE, 0, sign_token(self.key, data), deadline)
                    signed = True
                elif not offered:  # the TV does not know this key: offer it and wait for the user's answer
                    self._send(A_AUTH, AUTH_RSAPUBLICKEY, 0, public_key_blob(self.key), deadline)
                    offered = True
                    deadline = time.monotonic() + approval_wait
                    if approval_wait <= 0:
                        raise NeedsApproval("the TV asks to allow debugging")
                else:
                    raise AdbError("the TV refused the key")
        except socket.timeout:
            self.close()
            if offered:
                raise NeedsApproval("the TV asks to allow debugging")
            raise AdbError("timed out while logging in")
        except Exception:
            self.close()
            raise

    def shell(self, command, timeout=4.0):
        """Run one shell command and return its output as text."""
        if not self.sock:
            raise AdbError("not connected")
        deadline = time.monotonic() + timeout
        self._next_id += 1
        local = self._next_id
        out, remote = bytearray(), 0
        try:
            self._send(A_OPEN, local, 0, b"shell:" + command.encode() + b"\0", deadline)
            while True:
                cmd, arg0, arg1, data = self._recv(deadline)
                if arg1 != local:
                    continue
                if cmd == A_OKAY:
                    remote = arg0
                elif cmd == A_WRTE:
                    out.extend(data)
                    remote = arg0
                    self._send(A_OKAY, local, remote, deadline=deadline)
                elif cmd == A_CLSE:
                    break
        except socket.timeout:
            self.close()
            raise AdbError("the command timed out")
        return out.decode("utf-8", "replace")
