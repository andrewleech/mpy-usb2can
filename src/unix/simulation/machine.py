"""
Minimal `machine` stand-in for running the application on the unix port.

Only the subset the application touches is modelled; anything that needs real
hardware belongs in an on-target test instead.
"""


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2
    PULL_DOWN = 3

    def __init__(self, id, mode=-1, pull=-1, value=0):
        self.id = id
        self.mode = mode
        self.pull = pull
        self._value = value

    def value(self, val=None):
        if val is None:
            return self._value
        self._value = int(bool(val))
        return None

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def __repr__(self):
        return "Pin({!r}, value={})".format(self.id, self._value)
