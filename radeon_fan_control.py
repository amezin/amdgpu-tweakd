import argparse
import configparser
import glob
import pathlib
import time


class HwmonDevice:
    def __init__(self, sysfs_path, config):
        def getfloat(key, default):
            value = config.pop(key, default)
            try:
                return float(value)
            except ValueError as ex:
                print(sysfs_path, key + ":", ex)
                return default

        def getsysfs(key, default):
            path = self.sysfs_path / key;
            try:
                return float(path.read_text()) / 1000.0
            except Exception as ex:
                print("Can't read {}: {}".format(path, ex))
                return float(default)

        self.sysfs_path = pathlib.Path(sysfs_path)
        self.pwm_path = self.sysfs_path / 'pwm1'
        self.temp_path = self.sysfs_path / 'temp1_input'
        self.pwm_min = getfloat('pwm_min', getsysfs('pwm1_min', 0.0))
        self.pwm_max = getfloat('pwm_max', getsysfs('pwm1_max', 255.0))
        self.temp_min = getfloat('temp_min', 40.0)
        self.temp_max = getfloat('temp_max', getsysfs('temp1_crit', 90000.0) / 1000.0 - 5.0)
        self.pwm_delta = self.pwm_max - self.pwm_min
        self.temp_delta = self.temp_max - self.temp_min
        self.curve_pow = getfloat('curve_pow', 2.0)

        for k in config.keys():
            print("Unknown parameter {!r}".format(k))

    @property
    def temp(self):
        return int(self.temp_path.read_bytes()) / 1000.0

    @property
    def pwm(self):
        return int(self.pwm_path.read_bytes())

    @pwm.setter
    def pwm(self, value):
        self.pwm_path.write_bytes(str(int(value)).encode('ascii'))

    def update(self):
        temp_fraction = min(1.0, max(0.0, (self.temp - self.temp_min)) / self.temp_delta);
        self.pwm = self.pwm_min + self.pwm_delta * pow(temp_fraction, self.curve_pow);


def main(*args, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=pathlib.Path)
    arg = parser.parse_args(*args, **kwargs)

    config = configparser.ConfigParser()
    config.read(arg.config)

    devices = {}

    while True:
        devices = { path: dev for path, dev in devices.items() if dev.sysfs_path.exists() }

        for section in config.sections():
            for resolved_path in glob.glob(section):
                if resolved_path not in devices:
                    devices[resolved_path] = HwmonDevice(resolved_path, config[section])

        for device in devices.values():
            device.update()

        time.sleep(1)


if __name__ == '__main__':
    main()
