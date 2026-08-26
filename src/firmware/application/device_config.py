"""
This file describes how to initialise a config and set parameter defaults.
As well as update parameters with the override.
"""
import logging

from structured_config import Structure, BoolField, FloatField, SelectionField as SF

LOG_LEVEL_CHOICES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class DeviceConfig(Structure):
    # BLINKY (TEMPLATE) #################################################

    on_time = FloatField(1.0)  # Amount of time the LED stays ON (s)
    off_time = FloatField(1.0)  # Amount of time the LED stays OFF (s)

    # BLINKY (TEMPLATE) #################################################

    autorun = BoolField(True)  # Should the device begin workflow on start

    log_level_console = SF(logging.DEBUG, LOG_LEVEL_CHOICES) | "Console log level threshold"
    log_level_file = SF(logging.INFO, LOG_LEVEL_CHOICES) | "File log level threshold"


default_config = DeviceConfig()
device_config = DeviceConfig("config/usb2can.json")


def log_all_config_differences(logger: logging.Logger):
    """Log the differences detected in the config override files compared to the
    defaults set in each configuration file."""
    config_mods = [(key, value) for key, value in device_config if default_config[key] != value]
    if config_mods:
        logger.info("Config changes from default:")
        for key, value in config_mods:
            logger.info(f" - {key}: {value}")
