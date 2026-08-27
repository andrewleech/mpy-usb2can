"""
Composition root: builds a working gs_usb device out of the parts.

`CanCore`, `GsUsbControl`, `GsUsbDataPlane` and `GsUsbUsbDevice` are each
independent of the others and each testable alone. This is the one place that
knows how they fit together, so nothing else has to.

Construction order is forced by a dependency that runs both ways. The data
plane submits its bulk transfers through `machine.USBDevice`, which only exists
once `GsUsbUsbDevice.apply()` has configured it; but `apply()` installs the
callbacks, and two of those belong to the data plane. The handlers registered
below are therefore this object's own methods, forwarding to a data plane that
is attached later. A transfer completing before then is dropped, which is
correct: nothing has been submitted yet, so there is nothing outstanding for it
to belong to.

`start()` deliberately does not run at import. Bringing the device up replaces
the built-in CDC, since the vendor interface has to be `bInterfaceNumber 0`,
and that takes the USB REPL with it. Whether a shipping build starts
automatically is still open; until it is settled this stays something a caller
asks for.

    from gs_usb.app import GsUsbApp
    app = GsUsbApp()
    app.start()
"""

import logging

import asyncio

from gs_usb import control as control_mod
from gs_usb import device as device_mod
from gs_usb import usb_device as usb_device_mod

import can_core


log = logging.getLogger("gs_usb.app")


class GsUsbApp:
    """A gs_usb device: CAN core, control plane, data plane and USB layer."""

    def __init__(self, num_channels=1, serial=None, echo_on_write=True, can_class=None):
        self.core = can_core.CanCore(num_channels=num_channels, can_class=can_class)
        self.control = control_mod.GsUsbControl(self.core)

        strs = {}
        if serial is not None:
            strs["serial"] = serial
        self.usb_descriptors = usb_device_mod.GsUsbUsbDevice(
            open_itf_handler=self._open_itf_cb,
            control_xfer_handler=self.control.control_xfer_cb,
            xfer_handler=self._xfer_cb,
            reset_handler=self._reset_cb,
            **strs,
        )

        self._echo_on_write = echo_on_write
        self.usb = None
        self.plane = None

    # -- callbacks registered before the data plane exists ------------------

    def _open_itf_cb(self, itf_desc):
        if self.plane is not None:
            self.plane.open_itf_cb(itf_desc)

    def _xfer_cb(self, ep, result, xferred_bytes):
        if self.plane is not None:
            return self.plane.xfer_cb(ep, result, xferred_bytes)
        return False

    def _reset_cb(self):
        # Order matters: the control plane forgets that a channel was
        # running, and the data plane discards the transfers that were
        # outstanding for it. Clearing the control state first means the
        # data plane cannot briefly see a channel it believes is started
        # while its endpoints are already torn down.
        self.control.reset()
        if self.plane is not None:
            self.plane.reset()

    # -- lifecycle ----------------------------------------------------------

    def start(self, usb_device=None):
        """Configure USB, attach the data plane and enumerate.

        Returns the asyncio task the data plane needs running; the caller
        adds it to whatever loop it owns.
        """
        self.usb = self.usb_descriptors.apply(usb_device)
        self.plane = device_mod.GsUsbDataPlane(
            self.core, self.usb, echo_on_write=self._echo_on_write
        )
        self.usb.active(True)
        self.plane.start()
        log.info("gs_usb device active, %d channel(s)", self.core.num_channels)
        return self.plane.task

    def stop(self):
        """Take the device off the bus and stop every channel."""
        if self.plane is not None:
            self.plane.stop()
        for channel in range(self.core.num_channels):
            if self.core.is_started(channel):
                self.core.stop(channel)
        if self.usb is not None:
            self.usb.active(False)
        log.info("gs_usb device stopped")

    async def task(self):
        """Run the data plane's periodic work; start() must have been called."""
        if self.plane is None:
            raise RuntimeError("start() first")
        await self.plane.task()


def run(num_channels=1, serial=None):
    """Bring a device up on the running event loop and return it."""
    app = GsUsbApp(num_channels=num_channels, serial=serial)
    app.start()
    asyncio.create_task(app.task())
    return app
