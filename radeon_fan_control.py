import argparse
import asyncio
import configparser
import pathlib
import signal
import logging
import sys

import pyudev

import jeepney.bus_messages
import jeepney.integrate.asyncio


class HwmonDevice:
    def __init__(self, sysfs_path, config):
        config = dict(config)

        def getconfig(key, default):
            value = config.pop(key, default)
            try:
                return type(default)(value)
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
        self.pwm_min = getconfig('pwm_min', getsysfs('pwm1_min', 0.0))
        self.pwm_max = getconfig('pwm_max', getsysfs('pwm1_max', 255.0))
        self.temp_min = getconfig('temp_min', 40.0)
        self.temp_max = getconfig('temp_max', getsysfs('temp1_crit', 90000.0) / 1000.0 - 5.0)
        self.pwm_delta = self.pwm_max - self.pwm_min
        self.temp_delta = self.temp_max - self.temp_min
        self.curve_pow = getconfig('curve_pow', 2.0)
        self.semi_passive = getconfig('semi_passive', False)
        self.semi_passive_hyst = getconfig('semi_passive_hyst', 5.0)
        self.passive = False

        for k in config.keys():
            if k.lower() not in DeviceMatchingInfo.ATTRS:
                if k.upper() not in DeviceMatchingInfo.PROPS:
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

        for hwmon_device in udev.list_devices(subsystem='hwmon', parent=device):
            devices[hwmon_device.sys_path] = HwmonDevice(hwmon_device.sys_path, section)

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

    config = configparser.ConfigParser()
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
