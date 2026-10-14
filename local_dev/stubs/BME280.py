"""Simulate sensor readings without emulating BME280 I2C registers."""


class BME280:
    def __init__(self, i2c=None, **kwargs):
        self.i2c = i2c

    @property
    def values(self):
        return {"temperature": 22.5, "pressure": 1013.25, "humidity": 45.0}
