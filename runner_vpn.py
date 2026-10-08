import asyncio
import os
import secrets
import websockets
from cryptography.exceptions import InvalidTag
from Tun import TUNInterface
from vpn_crypto import MAGIC, MAX_PACKET, PacketCipher, encrypt_packet, decrypt_packet, load_key

def get_port():
    value = os.getenv("VPN_PORT", "8765")
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError("VPN_PORT must be an integer between 1 and 65535")
    return int(value)


async def tun_to_ws(ws, tun, cipher):
    loop = asyncio.get_running_loop()
    while True:
        ready = loop.create_future()
        def on_ready():
            if not ready.done():
                ready.set_result(None)
        loop.add_reader(tun.fd, on_ready)
        try:
            await ready
            packet = tun.read(MAX_PACKET)
        finally:
            loop.remove_reader(tun.fd)
        await ws.send(encrypt_packet(packet, cipher))


async def handler(ws, tun, connection_lock, key):
    # Authenticate before taking the single-client TUN lock.
    try:
        server_random = secrets.token_bytes(32)
        await ws.send(MAGIC + server_random)
        response = await asyncio.wait_for(ws.recv(), timeout=10)
        if (not isinstance(response, bytes) or len(response) != 64
                or response[:4] != MAGIC):
            raise ValueError("Invalid handshake")
        cipher = PacketCipher(key, server_random, response[4:36], server=True)
        if decrypt_packet(response[36:], cipher) != b"":
            raise ValueError("Invalid authentication proof")
    except (InvalidTag, ValueError, asyncio.TimeoutError):
        await ws.close(code=1008, reason="VPN authentication failed")
        return
    except websockets.exceptions.ConnectionClosed:
        return
    if connection_lock.locked():
        await ws.close(code=1013, reason="The TUN interface already has a client")
        return
    await connection_lock.acquire()
    send_task = None
    try:
        await ws.send(encrypt_packet(b"", cipher))
        send_task = asyncio.create_task(tun_to_ws(ws, tun, cipher))
        # Closing the socket also wakes the receiver if the TUN sender fails.
        async def close_on_sender_failure():
            try:
                await send_task
            except (ValueError, OSError):
                await ws.close(code=1011, reason="VPN sender failed; reconnect")
            except websockets.exceptions.ConnectionClosed:
                pass
        monitor = asyncio.create_task(close_on_sender_failure())
        async for frame in ws:
            packet = decrypt_packet(frame, cipher)
            if not packet:
                raise ValueError("Empty IP packet")
            tun.write(packet)
    except (InvalidTag, ValueError):
        await ws.close(code=1008, reason="Invalid encrypted VPN packet")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        try:
            if send_task is not None:
                send_task.cancel()
                monitor.cancel()
                await asyncio.gather(send_task, monitor, return_exceptions=True)
        finally:
            connection_lock.release()

async def main():
    port = get_port()
    key = load_key()
    tun = TUNInterface(name="iot0", address="10.0.0.1/24")
    try:
        tun.up()
        connection_lock = asyncio.Lock()
        async def handle_connection(ws):
            await handler(ws, tun, connection_lock, key)
        async with websockets.serve(handle_connection, "0.0.0.0", port,
                                    compression=None, max_size=MAX_PACKET + 28):
            print(f"IoT Gateway WebSocket listening on 0.0.0.0:{port}", flush=True)
            await asyncio.Future()
    finally:
        tun.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
