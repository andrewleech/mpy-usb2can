# -*- coding: utf-8 -*-
"""
Any module / object imported here will be immediately available for use within aiorepl.
Import or define additional things here to make software & systems testing quicker and easier.
"""

# This file provides the "global" environment for aiorepl
import asyncio
import logging
import os

import hardware

from device_config import device_config


log = logging.getLogger("repl")

# Linters / mypy recognise imports declared here as "used" and valid for the module.
__all__ = [
    # Modules
    "os",
    "device_config",
    "hardware",
]


def start():
    # Start the async repl with direct access to the globals in this module.
    import aiorepl

    log.info("Starting aiorepl")
    asyncio.create_task(aiorepl.task(globals()))
