#!/usr/bin/env python3
"""Send a file through the encrypted IoT Nexus VPN tunnel.

Example:
    python3 vpn_file_sender.py 203.0.113.10 8765 KEY --file sample.txt
"""
import argparse
import asyncio
import os
from pathlib import Path

import websockets

from vpn_crypto import MAGIC, MAX_PACKET, PacketCipher, decrypt_packet, encrypt_packet, parse_key

FILE_MAGIC = b"INX-FILE-1\0"


async def discard_server_packets(ws, cipher):
    """Drain the server broadcast so a sender cannot block the shared hub."""
    async for frame in ws:
        decrypt_packet(frame, cipher)


async def send_file(server, port, key, path):
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

        drain = asyncio.create_task(discard_server_packets(ws, cipher))
        try:
            name = path.name.encode("utf-8")
            if len(name) > 255:
                raise ValueError("File name is too long")
            header = FILE_MAGIC + bytes((len(name),)) + name
            with path.open("rb") as stream:
                first = True
                while True:
                    chunk = stream.read(MAX_PACKET - len(header))
                    if not chunk:
                        if first:
                            await ws.send(encrypt_packet(header, cipher))
                        break
                    first = False
                    await ws.send(encrypt_packet(header + chunk, cipher))
            print(f"Sent {path} through VPN to {server}:{port}")
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", help="VPN server IP address or hostname")
    parser.add_argument("port", type=int, help="VPN WebSocket port")
    parser.add_argument("key", help="shared key: exactly 64 hexadecimal characters")
    parser.add_argument("--file", required=True, type=Path, help="file to send")
    args = parser.parse_args()
    if not args.file.is_file():
        parser.error(f"file not found: {args.file}")
    try:
        asyncio.run(send_file(args.server, args.port, parse_key(args.key), args.file))
    except (OSError, ValueError, RuntimeError, websockets.WebSocketException) as error:
        parser.exit(1, f"VPN file send failed: {error}\n")


if __name__ == "__main__":
    main()
