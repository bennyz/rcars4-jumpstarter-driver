from dataclasses import dataclass

from jumpstarter_driver_composite.driver import CompositeInterface, Proxy

from jumpstarter.driver import Driver, export


@dataclass(kw_only=True)
class RCarSetup(CompositeInterface, Driver):
    """RCar Setup Driver"""
    log_level: str = "INFO"

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_rcars4.client.RCarSetupClient"

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        self.children["tftp"] = Proxy(ref="tftp_driver")
        self.children["http"] = Proxy(ref="http_driver")
        self.children["serial"] = Proxy(ref="serial_driver")
        self.children["power"] = Proxy(ref="power_driver")

    @export
    def power_cycle(self) -> dict:
        """Power cycle the device"""
        self.logger.info("Power cycling RCar device...")
        self.children["power"].off()

        import time
        time.sleep(3)

        self.children["power"].on()

        return {
            "tftp_host": self.children["tftp"].get_host(),
            "http_url": self.children["http"].get_url()
        }

    def close(self):
        """Cleanup resources"""
        if hasattr(super(), "close"):
            super().close()
