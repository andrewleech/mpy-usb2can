"""
gs_usb: an implementation of the Linux gs_usb / candleLight USB-CAN protocol.

protocol.py is the pure-computation layer: struct layouts, flag bits and the
frame/control payload codecs, with no USB, no CAN hardware and no asyncio.
control.py and device.py are the USB-facing layers built on it, driving a
CanCore instance over the control and bulk endpoints respectively; usb_device.py
assembles the USB descriptors and callback dispatch both of them sit behind.
"""
