class TimerChannel:
    def __init__(self, id):
        self.id = id

    def pulse_width_percent(self, value=None):
        pass


class Timer:
    PWM = 0

    def init(self, *args, **kwargs):
        pass

    def channel(self, id, *args, **kwargs):
        return TimerChannel(id)
