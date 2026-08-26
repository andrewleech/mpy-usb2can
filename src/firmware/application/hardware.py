import blink
from device_config import device_config as config
from machine import Pin

BLINK_PIN = "LED_GREEN"

pin_led_green = Pin(BLINK_PIN, Pin.OUT)

led_controller = blink.LEDController("green", pin_led_green)

blinky = blink.Blink(led_controller, config.on_time, config.off_time)


class DeviceCollection:
    # It is desirable to have these names be the same in the collection as
    # they are in the hardware module.
    # pylint: disable=redefined-outer-name
    def __init__(self, blinky):
        self.blinky = blinky


def shutdown():
    blinky.stop()


devices = DeviceCollection(blinky=blinky)


class SerialNumber:
    """
    SerialNumber class
    TODO Replace with the Production module
    """

    _serial_number = "usb2can_0"
    MAX_LENGTH = 64

    @classmethod
    def read(cls):
        return cls._serial_number

    @classmethod
    async def write(cls, data):
        serial_str = data.decode("utf-8").strip() if isinstance(data, bytes) else str(data).strip()

        if not serial_str:
            raise ValueError("Serial number cannot be empty")
        if len(serial_str) > cls.MAX_LENGTH:
            raise ValueError(f"Serial number too long (max {cls.MAX_LENGTH} characters)")

        cls._serial_number = serial_str
