import argparse
import asyncio
import collections.abc
import configparser
import contextlib
import pathlib
import signal
import logging
import sys

import pyudev

import jeepney.bus_messages
import jeepney.integrate.asyncio


class DeviceConfig(collections.abc.Mapping):
    def __init__(self, section):
        super().__init__()
        self.section = section

    def warn_unused_options(self):
        used_options = self.section.parser.used_options[self.name]
        for k in self.keys():
            if k not in used_options:
                logging.warning("Unknown option %r in section %r (known options are %r)", self.name, k, used_options)

    @property
    def name(self):
        return self.section.name

    def __getitem__(self, item):
        return self.section[item]

    def __iter__(self):
        return iter(self.section)

    def __len__(self):
        return len(self.section)

    def get(self, option, fallback=None, **kwargs):
        return self.section.get(option, fallback, **kwargs)

    @contextlib.contextmanager
    def wrap_value_errors(self, option):
        try:
            yield
        except ValueError as ex:
            raise ValueError("%r in %r: %s", option, self.name, ex) from ex

    def getfloat(self, option, fallback=None, **kwargs):
        with self.wrap_value_errors(option):
            return self.section.getfloat(option, fallback, **kwargs)

    def getboolean(self, option, fallback=None, **kwargs):
        with self.wrap_value_errors(option):
            return self.section.getboolean(option, fallback, **kwargs)

    def getint(self, option, fallback=None, **kwargs):
        with self.wrap_value_errors(option):
            return self.section.getint(option, fallback, **kwargs)


class Config(configparser.ConfigParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.used_options = {}

    def get(self, section, option, **kwargs):
        if section not in self.used_options:
            self.used_options[section] = set()
        self.used_options[section].add(self.optionxform(option))
        return super().get(section, option, **kwargs)


class HwmonDevice:
    def __init__(self, sysfs_path, config):
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
        self.pwm_min = config.getfloat('pwm_min', getsysfs('pwm1_min', 0.0))
        self.pwm_max = config.getfloat('pwm_max', getsysfs('pwm1_max', 255.0))
        self.temp_min = config.getfloat('temp_min', 40.0)
        self.temp_max = config.getfloat('temp_max', getsysfs('temp1_crit', 90000.0) / 1000.0 - 5.0)
        self.pwm_delta = self.pwm_max - self.pwm_min
        self.temp_delta = self.temp_max - self.temp_min
        self.curve_pow = config.getfloat('curve_pow', 2.0)
        self.semi_passive = config.getboolean('semi_passive', False)
        self.semi_passive_hyst = config.getfloat('semi_passive_hyst', 5.0)
        self.passive = False
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

        if self.semi_passive and self.temp < self.temp_min:
            self.pwm = 0
            self.passive = True
            return

        if self.passive and self.temp < self.temp_min + self.semi_passive_hyst:
            self.pwm = 0
            return

        self.passive = False
        temp_fraction = min(1.0, max(0.0, (self.temp - self.temp_min)) / self.temp_delta);
        self.pwm = self.pwm_min + self.pwm_delta * pow(temp_fraction, self.curve_pow);

    def restore_pwm_enable(self):
        if self.prev_pwm_enable is not None:
            self.pwm_enable = self.prev_pwm_enable


class DeviceMatchingInfo:
    ATTRS = ['device', 'vbios_version']
    PROPS = ['PCI_ID', 'PCI_SLOT_NAME', 'PCI_SUBSYS_ID']
    UDEV_ATTR_ENCODING = sys.getfilesystemencoding()

    def __init__(self, device):
        self.props = {
            p: device.properties.get(p) for p in self.PROPS
        }
        self.attrs = {
            a: device.attributes.get(a) for a in self.ATTRS
        }

    def match(self, section):
        match_props = {p: section[p] for p in self.props.keys() if p in section}
        match_attrs = {a: section[a] for a in self.attrs.keys() if a in section}

        for p, v in match_props.items():
            if v != self.props[p]:
                return -1

        for a, v in match_attrs.items():
            if v.encode(self.UDEV_ATTR_ENCODING) != self.attrs[a]:
                return -1

        return len(match_props) + len(match_attrs)

    def best_match(self, config):
        best_section, best_score = None, -1

        for section_name in config.sections():
            section = config[section_name]
            score = self.match(section)

            if score > best_score:
                best_section, best_score = section, score

        return best_section, best_score


async def update_loop(config, udev):
    devices = {}

    for device in udev.list_devices(DRIVER='amdgpu'):
        section, score = DeviceMatchingInfo(device).best_match(config)
        if section is None:
            continue

        logging.info("Matched config %r to %r (score %d)", section.name, device, score)

        device_config = DeviceConfig(section)
        for hwmon_device in udev.list_devices(subsystem='hwmon', parent=device):
            try:
                devices[hwmon_device.sys_path] = HwmonDevice(hwmon_device.sys_path, device_config)
            except Exception:
                logging.exception("Failed to enable fan control for device %s", hwmon_device.sys_path)

        device_config.warn_unused_options()

    try:
        while True:
            await asyncio.sleep(1)

            for device in devices.values():
                try:
                    device.update()
                except Exception:
                    logging.exception("Failed to update device %s", device.sysfs_path)

    finally:
        for dev in devices.values():
            try:
                dev.restore_pwm_enable()
            except Exception:
                logging.exception("Failed to release device %s", dev.sysfs_path)


async def main_async(config):
    udev = pyudev.Context()

    wake_event = asyncio.Event()
    wake_event.set()

    update_loop_task = None

    def prepare_for_sleep(sleep):
        if sleep:
            logging.info("Preparing for sleep")
            wake_event.clear()
            if update_loop_task:
                update_loop_task.cancel()
        else:
            wake_event.set()
            logging.info("Woke up")

    _, dbus_proto = await jeepney.integrate.asyncio.connect_and_authenticate('SYSTEM')
    dbus_bus = jeepney.integrate.asyncio.Proxy(jeepney.bus_messages.message_bus, dbus_proto)

    dbus_proto.router.subscribe_signal(
        callback=lambda args: prepare_for_sleep(*args),
        path='/org/freedesktop/login1',
        interface='org.freedesktop.login1.Manager',
        member='PrepareForSleep'
    )

    await dbus_bus.AddMatch(jeepney.bus_messages.MatchRule(
        type='signal',
        sender='org.freedesktop.login1',
        interface='org.freedesktop.login1.Manager',
        member='PrepareForSleep',
        path='/org/freedesktop/login1'
    ))

    while True:
        logging.info("Waiting for wake event")
        await wake_event.wait()
        logging.info("Starting update loop")
        update_loop_task = asyncio.ensure_future(update_loop(config, udev))
        try:
            await update_loop_task
        except asyncio.CancelledError:
            logging.info("Stopped update loop")
            if wake_event.is_set():
                raise


def main(*args, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=pathlib.Path)
    arg = parser.parse_args(*args, **kwargs)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = Config()
    config.read(arg.config)

    main_loop = asyncio.get_event_loop()
    main_task = main_loop.create_task(main_async(config))

    main_loop.add_signal_handler(signal.SIGINT, main_task.cancel)
    main_loop.add_signal_handler(signal.SIGTERM, main_task.cancel)

    try:
        main_loop.run_until_complete(main_task)

    except asyncio.CancelledError:
        pass


if __name__ == '__main__':
    main()
