# -*- coding: utf-8 -*-
"""
The application entry point.
"""
import logging

import asyncio

from device_config import device_config
from hardware import devices
import version


log = logging.getLogger("device")


def run():
    """Start running main application"""
    log.info("Firmware version %s", version.firmware_version)

    loop = asyncio.get_event_loop()

    # Start interactive REPL
    import repl

    repl.start()

    # This is the list of top level tasks that the device runs.
    tasks = [devices.blinky.task]

    for task in tasks:
        if task is not None:
            loop.create_task(task())

    if device_config.autorun:
        devices.blinky.start()

    loop.run_forever()
