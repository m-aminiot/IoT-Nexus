"""IoT Nexus v1 packet encryption and private pre-shared key management."""
import argparse
import getpass
import hashlib
import os
from pathlib import Path
import secrets
import stat
import tempfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"INX1"
KEY_FILE = Path("/etc/iot-gateway/vpn.key")
MAX_PACKET = 65535
# Reconnect before exhausting a session's packet budget.
MAX_SEQUENCE = 2**32


class PacketCipher:
    """One context per connection; never share it between connections/tasks."""

    def __init__(self, key, server_random, client_random, *, server):
        if len(key) != 32 or len(server_random) != 32 or len(client_random) != 32:
            raise ValueError("Keys and handshake random values must be 32 bytes")
        material = HKDF(algorithm=hashes.SHA256(), length=64,
                        salt=server_random + client_random,
                        info=b"iot-nexus/v1/packet-keys").derive(key)
        s2c, c2s = material[:32], material[32:]
        self.tx = AESGCM(s2c if server else c2s)
        self.rx = AESGCM(c2s if server else s2c)
        self.tx_sequence = 0
        self.rx_sequence = 0


def encrypt_packet(packet, cipher):
    """Return a 12-byte sequence nonce followed by ciphertext and a 16-byte tag."""
    if not isinstance(packet, bytes) or len(packet) > MAX_PACKET:
        raise ValueError("Invalid IP packet size/type")
    if cipher.tx_sequence >= MAX_SEQUENCE:
        raise ValueError("Session exhausted; reconnect")
    nonce = cipher.tx_sequence.to_bytes(12, "big")
    frame = nonce + cipher.tx.encrypt(nonce, packet, MAGIC)
    cipher.tx_sequence += 1
    return frame


def decrypt_packet(frame, cipher):
    """Authenticate and reject replay/out-of-order frames before returning plaintext."""
    if not isinstance(frame, bytes) or not 28 <= len(frame) <= MAX_PACKET + 28:
        raise ValueError("Invalid encrypted frame")
    nonce = frame[:12]
    if (cipher.rx_sequence >= MAX_SEQUENCE or
            int.from_bytes(nonce, "big") != cipher.rx_sequence):
        raise ValueError("Unexpected packet sequence")
    packet = cipher.rx.decrypt(nonce, frame[12:], MAGIC)
    cipher.rx_sequence += 1
    return packet


def parse_key(value):
    value = value.strip()
    if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise ValueError("Key must contain exactly 64 hexadecimal characters (32 bytes)")
    return bytes.fromhex(value)


def load_key(path=KEY_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError("VPN key must be a regular file owned by the service user with mode 600")
        if metadata.st_size not in (64, 65):
            raise ValueError("Invalid VPN key file length")
        return parse_key(stream.read(66))


def save_key(key, path=KEY_FILE):
    """Atomically replace the key in an administrator-owned directory."""
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes")
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".vpn-key-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(key.hex() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "status", "show", "set", "generate"))
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not os.path.lexists(KEY_FILE):
                save_key(secrets.token_bytes(32))
            load_key()
        elif args.action == "set":
            save_key(parse_key(getpass.getpass("New key (64 hex characters): ")))
        elif args.action == "generate":
            save_key(secrets.token_bytes(32))
        elif args.action == "show":
            print(load_key().hex())
        else:
            print("AES-256-GCM; key fingerprint: " + hashlib.sha256(load_key()).hexdigest()[:16])
    except (OSError, ValueError, EOFError) as exc:
        parser.exit(1, f"Key operation failed: {exc}\n")


if __name__ == "__main__":
    main()
