import logging

import asyncio
from machine import Pin

logger = logging.getLogger("blinky")

ON = 1
OFF = 0


class LEDController:
    def __init__(self, name, pin: Pin):
        self.name = name
        self.pin = pin

    def on(self):
        self.pin.value(ON)

    def off(self):
        self.pin.value(OFF)


class Blink:
    """Example class implementing async LED blink."""

    def __init__(self, led: LEDController, on_time, off_time):
        self.led = led
        self.on_time = on_time
        self.off_time = off_time
        self.run = asyncio.Event()

    async def task(self):
        """
        Blinky task
        Will not be used if a state machine is selected.
        """
        logger.info("Starting Blinky task")
        while True:
            await self.run.wait()

            logger.info("blink!")

            self.led.on()
            await asyncio.sleep(self.on_time)

            self.led.off()
            await asyncio.sleep(self.off_time)

    def start(self):
        self.run.set()

    def stop(self):
        self.run.clear()
