#!/usr/bin/env python3
"""Wait for and display files received through the encrypted IoT Nexus VPN.

Example (run on the VPN server):
    python3 vpn_file_receiver.py KEY --output ./received
"""
import argparse
import asyncio
import os
from pathlib import Path

import websockets

from vpn_crypto import MAGIC, MAX_PACKET, PacketCipher, decrypt_packet, encrypt_packet, parse_key

FILE_MAGIC = b"INX-FILE-1\0"


async def receive_files(server, port, key, output):
    output.mkdir(parents=True, exist_ok=True)
    async with websockets.connect(f"ws://{server}:{port}", compression=None,
                                  max_size=MAX_PACKET + 28) as ws:
        hello = await ws.recv()
        if not isinstance(hello, bytes) or hello[:4] != MAGIC or len(hello) != 36:
            raise RuntimeError("Invalid VPN server handshake")
        client_random = os.urandom(32)
        cipher = PacketCipher(key, hello[4:], client_random, server=False)
        await ws.send(MAGIC + client_random + encrypt_packet(b"", cipher))
        if decrypt_packet(await ws.recv(), cipher) != b"":
            raise RuntimeError("VPN server authentication failed")
        print(f"Listening for VPN messages from {server}:{port} (Ctrl-C to stop)", flush=True)
        while True:
            payload = decrypt_packet(await ws.recv(), cipher)
            if not payload.startswith(FILE_MAGIC) or len(payload) <= len(FILE_MAGIC):
                print(f"Received {len(payload)} bytes: {payload!r}", flush=True)
                continue
            name_length = payload[len(FILE_MAGIC)]
            start = len(FILE_MAGIC) + 1
            name = payload[start:start + name_length].decode("utf-8", "replace")
            data = payload[start + name_length:]
            destination = output / Path(name).name
            with destination.open("ab") as stream:
                stream.write(data)
            print(f"Received {len(data)} bytes for {destination}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", help="shared key: exactly 64 hexadecimal characters")
    parser.add_argument("--server", default="127.0.0.1",
                        help="VPN server address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765,
                        help="VPN WebSocket port (default: 8765)")
    parser.add_argument("--output", type=Path, default=Path("received-vpn-files"),
                        help="directory for received files")
    args = parser.parse_args()
    try:
        asyncio.run(receive_files(args.server, args.port, parse_key(args.key), args.output))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, websockets.WebSocketException) as error:
        parser.exit(1, f"VPN file receiver failed: {error}\n")


if __name__ == "__main__":
    main()
