import argparse
import configparser
import glob
import pathlib
import signal
import time
import logging

from gi.repository import GLib


class HwmonDevice:
    def __init__(self, sysfs_path, config):
        def getfloat(key, default):
            value = config.pop(key, default)
            try:
                return float(value)
            except ValueError as ex:
                logging.error("%s.%s: %s", sysfs_path, key, ex)
                return default

        def getsysfs(key, default):
            path = self.sysfs_path / key;
            try:
                return float(path.read_text())
            except Exception as ex:
                logging.error("Can't read %s: %s", path, ex)
                return float(default)

        self.sysfs_path = pathlib.Path(sysfs_path)
        self.pwm_path = self.sysfs_path / 'pwm1'
        self.pwm_enable_path = self.sysfs_path / 'pwm1_enable'
        self.temp_path = self.sysfs_path / 'temp1_input'
        self.pwm_min = getfloat('pwm_min', getsysfs('pwm1_min', 0.0))
        self.pwm_max = getfloat('pwm_max', getsysfs('pwm1_max', 255.0))
        self.temp_min = getfloat('temp_min', 40.0)
        self.temp_max = getfloat('temp_max', getsysfs('temp1_crit', 90000.0) / 1000.0 - 5.0)
        self.pwm_delta = self.pwm_max - self.pwm_min
        self.temp_delta = self.temp_max - self.temp_min
        self.curve_pow = getfloat('curve_pow', 2.0)

        for k in config.keys():
            logging.warning("Unknown parameter %r", k)

        self.prev_pwm_enable = None

        logging.info("Created device: %r", self.__dict__)

    @property
    def temp(self):
        return int(self.temp_path.read_bytes()) / 1000.0

    @property
    def pwm(self):
        return int(self.pwm_path.read_bytes())

    @pwm.setter
    def pwm(self, value):
        self.pwm_path.write_bytes(str(int(value)).encode('ascii'))

    @property
    def pwm_enable(self):
        return self.pwm_enable_path.read_bytes().rstrip()

    @pwm_enable.setter
    def pwm_enable(self, value):
        self.pwm_enable_path.write_bytes(value)

    def update(self):
        cur_pwm_enable = self.pwm_enable
        if cur_pwm_enable != b'1':
            self.prev_pwm_enable = cur_pwm_enable
            self.pwm_enable = b'1'
            logging.info("Enabled fan speed control for %s", self.sysfs_path)

        temp_fraction = min(1.0, max(0.0, (self.temp - self.temp_min)) / self.temp_delta);
        self.pwm = self.pwm_min + self.pwm_delta * pow(temp_fraction, self.curve_pow);

    def restore_pwm_enable(self):
        if self.prev_pwm_enable is not None:
            self.pwm_enable = self.prev_pwm_enable


class FanControlService:
    def __init__(self, config, main_context):
        self.config = config
        self.devices = {}
        self.timeout_source = None
        self.main_context = main_context

    def update(self):
        self.devices = { path: dev for path, dev in self.devices.items() if dev.sysfs_path.exists() }

        for section in self.config.sections():
            for resolved_path in glob.glob(section):
                if resolved_path not in self.devices:
                    self.devices[resolved_path] = HwmonDevice(resolved_path, self.config[section])

        for device in self.devices.values():
            device.update()

        self.schedule_update()

    def restore_pwm_enable(self):
        for dev in self.devices.values():
            dev.restore_pwm_enable()

    def schedule_update(self):
        self.timeout_source = GLib.timeout_source_new_seconds(1)
        self.timeout_source.set_callback(lambda _: self.update())
        self.timeout_source.attach(self.main_context)


def main(*args, **kwargs):
    main_loop = GLib.MainLoop()

    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=pathlib.Path)
    arg = parser.parse_args(*args, **kwargs)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = configparser.ConfigParser()
    config.read(arg.config)

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, main_loop.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, main_loop.quit)

    service = FanControlService(config, main_loop.get_context())
    service.schedule_update()

    try:
        main_loop.run()
    finally:
        service.restore_pwm_enable()


if __name__ == '__main__':
    main()
