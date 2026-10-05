import logging
import os
import struct
import subprocess
from fcntl import ioctl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_TUNSETIFF = 0x400454ca
_IFF_TUN = 0x0001
_IFF_NO_PI = 0x1000

class TUNInterface:
    def __init__(self, name="tun0", address="10.0.0.1/24"):
        self._name = name
        self._address = address
        self._descriptor = os.open("/dev/net/tun", os.O_RDWR)
        ifr = struct.pack("16sH", name.encode("ASCII"), _IFF_TUN | _IFF_NO_PI)
        ioctl(self._descriptor, _TUNSETIFF, ifr)
        subprocess.run(["ip", "addr", "add", self._address, "dev", self._name], check=True)
        logging.info(f"Interface {self._name} created with address {self._address}")

    @property
    def name(self):
        return self._name

    @property
    def fd(self):
        return self._descriptor

    def up(self):
        subprocess.run(["ip", "link", "set", "dev", self._name, "up"], check=True)
        logging.info(f"Interface {self._name} is UP")

    def read(self, n=2048):
        return os.read(self._descriptor, n)

    def write(self, packet):
        os.write(self._descriptor, packet)

    def close(self):
        os.close(self._descriptor)
        logging.info(f"Interface {self._name} closed")
