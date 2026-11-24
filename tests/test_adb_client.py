import base64
import os
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from support import ROOT  # noqa: F401  (sets up sys.path)

import netcec_adb as adb

_KEY = None


def key():
    """One key for all tests (generating takes a moment)."""
    global _KEY
    if _KEY is None:
        _KEY = adb.generate_key()
    return _KEY


def parse_blob(blob):
    raw64, name = blob.rstrip(b"\0").split(b" ", 1)
    raw = base64.b64decode(raw64)
    words, n0inv = struct.unpack("<II", raw[:8])
    n = int.from_bytes(raw[8:8 + 256], "little")
    rr = int.from_bytes(raw[8 + 256:8 + 512], "little")
    (e,) = struct.unpack("<I", raw[8 + 512:])
    return words, n0inv, n, rr, e, name


class KeyAndSignature(unittest.TestCase):
    def test_the_key_is_2048_bits_and_works(self):
        k = key()
        self.assertEqual(k["n"].bit_length(), 2048)
        self.assertEqual(k["e"], 65537)
        m = 123456789123456789
        self.assertEqual(pow(pow(m, k["e"], k["n"]), k["d"], k["n"]), m)

    def test_the_public_key_blob_has_the_android_layout(self):
        k = key()
        blob = adb.public_key_blob(k, "me@here")
        words, n0inv, n, rr, e, name = parse_blob(blob)
        self.assertTrue(blob.endswith(b"\0"))
        self.assertEqual((words, e, name), (64, 65537, b"me@here"))
        self.assertEqual(n, k["n"])
        self.assertEqual((n0inv * (n & 0xFFFFFFFF)) % (1 << 32), (1 << 32) - 1)  # n0inv = -1 / n[0] mod 2^32
        self.assertEqual(rr, pow(1 << 2048, 2, n))

    def test_the_signature_is_pkcs1_with_the_sha1_prefix_around_the_token(self):
        k, token = key(), bytes(range(20))
        em = pow(int.from_bytes(adb.sign_token(k, token), "big"), k["e"], k["n"]).to_bytes(256, "big")
        self.assertTrue(em.startswith(b"\x00\x01\xff\xff"))
        self.assertTrue(em.endswith(b"\x00" + adb._SHA1_PREFIX + token))
        self.assertEqual(set(em[2:-(1 + len(adb._SHA1_PREFIX) + 20)]), {0xFF})

    def test_the_key_file_is_private_and_reloaded(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "adbkey.json")
        with mock.patch.object(adb, "generate_key", return_value=key()):
            first = adb.load_or_create_key(path)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(adb.load_or_create_key(path), first)  # no new key the second time

    def test_prime_check(self):
        self.assertTrue(adb._is_probable_prime(2 ** 127 - 1))
        self.assertFalse(adb._is_probable_prime(2 ** 127 + 1))
        self.assertFalse(adb._is_probable_prime(561))  # a Carmichael number


def recv_message(conn):
    header = b""
    while len(header) < 24:
        chunk = conn.recv(24 - len(header))
        if not chunk:
            raise ConnectionError
        header += chunk
    cmd, a0, a1, length, _check, magic = struct.unpack("<6I", header)
    assert magic == cmd ^ 0xFFFFFFFF
    data = b""
    while len(data) < length:
        data += conn.recv(length - len(data))
    return cmd, a0, a1, data


class FakeAdbd:
    """A TV with adbd: asks for the key, shows a prompt for unknown keys, runs shell commands."""

    def __init__(self, outputs=None, silent=False):
        self.known, self.commands, self.outputs = [], [], outputs or {}
        self.approve = threading.Event()
        self.silent = silent  # never answers (for timeouts)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.srv.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _signature_ok(self, token, signature):
        for n, e in self.known:
            em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(256, "big")
            if em.endswith(b"\x00" + adb._SHA1_PREFIX + token) and em.startswith(b"\x00\x01\xff"):
                return True
        return False

    def _send(self, conn, cmd, a0, a1, data=b""):
        conn.sendall(adb._pack(cmd, a0, a1, data))

    def _handle(self, conn):
        try:
            if self.silent:
                time.sleep(30)
                return
            cmd, *_ = recv_message(conn)
            assert cmd == adb.A_CNXN
            token = os.urandom(20)
            self._send(conn, adb.A_AUTH, adb.AUTH_TOKEN, 0, token)
            cmd, a0, _a1, data = recv_message(conn)
            if not (cmd == adb.A_AUTH and a0 == adb.AUTH_SIGNATURE and self._signature_ok(token, data)):
                self._send(conn, adb.A_AUTH, adb.AUTH_TOKEN, 0, os.urandom(20))
                cmd, a0, _a1, data = recv_message(conn)
                assert cmd == adb.A_AUTH and a0 == adb.AUTH_RSAPUBLICKEY
                _, _, n, _, e, _ = parse_blob(data)
                if not self.approve.wait(10):  # the user on the TV decides
                    return
                self.known.append((n, e))
            self._send(conn, adb.A_CNXN, adb.A_VERSION, 4096, b"device::\0")
            while True:
                cmd, local, _zero, data = recv_message(conn)
                if cmd != adb.A_OPEN:
                    continue
                command = data.rstrip(b"\0").decode().split("shell:", 1)[1]
                self.commands.append(command)
                self._send(conn, adb.A_OKAY, 77, local)
                out = self.outputs.get(command, "")
                out = out if isinstance(out, bytes) else out.encode()
                for i in range(0, len(out), 100):  # in chunks, like a real device
                    self._send(conn, adb.A_WRTE, 77, local, out[i:i + 100])
                    recv_message(conn)  # the client acknowledges every chunk
                self._send(conn, adb.A_CLSE, 77, local)
        except (ConnectionError, OSError, AssertionError):
            pass
        finally:
            conn.close()


class Connection(unittest.TestCase):
    def setUp(self):
        self.tv = FakeAdbd(outputs={"echo hi": "hi\n", "big": "x" * 1000 + "\u00e4\u00f6\n"})
        self.addCleanup(self.tv.close)

    def connection(self, timeout=3.0):
        return adb.Connection("127.0.0.1", key(), port=self.tv.port, timeout=timeout)

    def test_a_known_key_logs_in_and_runs_commands(self):
        self.tv.known.append((key()["n"], key()["e"]))
        c = self.connection().connect()
        self.assertEqual(c.shell("echo hi"), "hi\n")
        self.assertEqual(self.tv.commands, ["echo hi"])
        c.close()

    def test_an_unknown_key_waits_for_the_user_on_the_tv(self):
        threading.Timer(0.3, self.tv.approve.set).start()
        c = self.connection().connect(approval_wait=5)
        self.assertEqual(c.shell("echo hi"), "hi\n")
        c.close()
        self.assertEqual(len(self.tv.known), 1)
        # the next time the key is known: no waiting, no prompt
        self.tv.approve.clear()
        self.connection().connect(approval_wait=0).close()

    def test_nobody_answers_the_prompt(self):
        with self.assertRaises(adb.NeedsApproval):
            self.connection().connect(approval_wait=0.4)

    def test_without_waiting_the_prompt_is_reported_at_once(self):
        t0 = time.monotonic()
        with self.assertRaises(adb.NeedsApproval):
            self.connection().connect(approval_wait=0)
        self.assertLess(time.monotonic() - t0, 2)

    def test_long_output_arrives_in_pieces_and_is_decoded(self):
        self.tv.known.append((key()["n"], key()["e"]))
        c = self.connection().connect()
        out = c.shell("big")
        self.assertEqual(out, "x" * 1000 + "\u00e4\u00f6\n")

    def test_several_commands_on_one_connection(self):
        self.tv.known.append((key()["n"], key()["e"]))
        c = self.connection().connect()
        c.shell("echo hi")
        c.shell("echo hi")
        self.assertEqual(self.tv.commands, ["echo hi", "echo hi"])

    def test_nothing_listens(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        with self.assertRaises(adb.AdbError):
            adb.Connection("127.0.0.1", key(), port=port, timeout=1).connect()

    def test_a_tv_that_never_answers_times_out(self):
        silent = FakeAdbd(silent=True)
        self.addCleanup(silent.close)
        t0 = time.monotonic()
        with self.assertRaises(adb.AdbError):
            adb.Connection("127.0.0.1", key(), port=silent.port, timeout=0.5).connect()
        self.assertLess(time.monotonic() - t0, 3)

    def test_a_command_that_hangs_is_cut_off(self):
        class Hang(FakeAdbd):
            def _send(self, conn, cmd, a0, a1, data=b""):
                if cmd == adb.A_WRTE:
                    time.sleep(5)
                super()._send(conn, cmd, a0, a1, data)

        tv = Hang(outputs={"echo hi": "hi"})
        self.addCleanup(tv.close)
        tv.known.append((key()["n"], key()["e"]))
        c = adb.Connection("127.0.0.1", key(), port=tv.port, timeout=3).connect()
        with self.assertRaises(adb.AdbError):
            c.shell("echo hi", timeout=0.4)
        self.assertFalse(c.alive)  # a half-read connection is never reused


if __name__ == "__main__":
    unittest.main()
