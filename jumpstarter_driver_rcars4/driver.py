from dataclasses import dataclass
import logging
import time

from jumpstarter.driver import Driver, export
from jumpstarter.drivers.composite.driver import CompositeInterface
from jumpstarter.drivers.pyserial.driver import PySerial

logger = logging.getLogger(__name__)

@dataclass(kw_only=True)
class RCarSetup(CompositeInterface, Driver):
    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_rcars4.client.RCarSetupClient"

    def __post_init__(self):
        super().__post_init__()
        if "serial" not in self.children:
            self.children["serial"] = PySerial(url="/dev/ttyUSB0", baudrate=1843200)

    @export
    def power_cycle(self) -> dict:
        logger.info("power cycling device...")
        self.children["gpio"].off()
        time.sleep(3)
        self.children["gpio"].on()

        return {
            "tftp_host": self.children["tftp"].get_host(),
            "http_url": self.children["http"].get_url()
        }
