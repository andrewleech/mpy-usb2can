# -*- coding: utf-8 -*-
"""
This module is run when the device powers on or is reset.

It is kept minimal so that the MCU peripherals and hardware is relatively
untouched on start up.  To execute the application do `run()` at the REPL.
"""
import logging

from device_config import device_config, log_all_config_differences


logging.basicConfig(level=device_config.log_level_console)
log = logging.getLogger("main")

log_all_config_differences(log)


def run():
    import device

    log.info("Running main application")
    device.run()


if device_config.autorun:
    run()
