import argparse
import asyncio
import codecs
import collections.abc
import configparser
import logging
import pathlib
import shutil
import signal
import shlex
import subprocess
import sys

import pyudev

import jeepney.bus_messages
import jeepney.integrate.asyncio


UDEV_ATTR_ENCODING = codecs.getencoder(sys.getfilesystemencoding())

DEVICE_CONFIG_FIELDS = {
    'device': lambda v: UDEV_ATTR_ENCODING(v)[0],
    'vbios_version': lambda v: UDEV_ATTR_ENCODING(v)[0],
    'pci_id': str,
    'pci_slot_name': str,
    'pci_subsys_id': str,
    'fan_control': bool,
    'fan_pwm_min': float,
    'fan_pwm_max': float,
    'temp_min': float,
    'temp_max': float,
    'fan_curve_pow': float,
    'fan_semi_passive': bool,
    'fan_semi_passive_hyst': float,
    'power_cap': int
}

DEVICE_CONFIG_DEFAULTS = {
    'fan_control': False,
    'temp_min': 40.0,
    'fan_curve_pow': 2.0,
    'fan_semi_passive': False,
    'fan_semi_passive_hyst': 5.0
}

DeviceConfig = collections.namedtuple('DeviceConfig', ['name'] + list(DEVICE_CONFIG_FIELDS.keys()))

PP_OVERDRIVE_MASK = 0x4000
ppfeaturemask_path = pathlib.Path('/sys/module/amdgpu/parameters/ppfeaturemask')


class FanController:
    def __init__(self, sysfs_path, config):
        def getsysfs(key, default):
            path = self.sysfs_path / key
            try:
                return float(path.read_text())
            except Exception as ex:
                logging.error("Can't read %s: %s", path, ex)
                return float(default)

        self.sysfs_path = pathlib.Path(sysfs_path)
        self.pwm_path = self.sysfs_path / 'pwm1'
        self.pwm_enable_path = self.sysfs_path / 'pwm1_enable'
        self.temp_path = self.sysfs_path / 'temp1_input'

        if config.fan_pwm_min is None:
            self.pwm_min = getsysfs('pwm1_min', 0.0)
        else:
            self.pwm_min = config.fan_pwm_min

        if config.fan_pwm_max is None:
            self.pwm_max = getsysfs('pwm1_max', 255.0)
        else:
            self.pwm_max = config.fan_pwm_max

        if config.temp_max is None:
            self.temp_max = getsysfs('temp1_crit', 90000.0) / 1000.0 - 5.0
        else:
            self.temp_max = config.temp_max

        self.temp_min = config.temp_min
        self.semi_passive = config.fan_semi_passive
        self.semi_passive_hyst = config.fan_semi_passive_hyst
        self.curve_pow = config.fan_curve_pow

        self.pwm_delta = self.pwm_max - self.pwm_min
        self.temp_delta = self.temp_max - self.temp_min
        self.turned_off = False
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
            self.turned_off = True
            return

        if self.turned_off and self.temp < self.temp_min + self.semi_passive_hyst:
            self.pwm = 0
            return

        self.turned_off = False
        temp_fraction = min(1.0, max(0.0, (self.temp - self.temp_min)) / self.temp_delta)
        self.pwm = self.pwm_min + self.pwm_delta * pow(temp_fraction, self.curve_pow)

    def restore_pwm_enable(self):
        if self.prev_pwm_enable is not None:
            self.pwm_enable = self.prev_pwm_enable


class SysfsOverride:
    def __init__(self):
        self.prev_values = {}

    def write(self, path, value, warn_overdrive=True):
        try:
            logging.info("Writing %r to %s", value, path)
            path.write_bytes(value)

        except Exception as ex:
            logging.error("Can't write %r to %s: %s", value, path, ex)

            if warn_overdrive:
                ppfeaturemask = int(ppfeaturemask_path.read_bytes())
                if not (ppfeaturemask & PP_OVERDRIVE_MASK):
                    logging.warning("Overdrive is currently disabled. Run 'amdgpu-unlock-overdrive' to enable it")

    def __setitem__(self, key, value):
        key = pathlib.Path(key)
        value = value.encode('ascii')

        try:
            if key not in self.prev_values:
                self.prev_values[key] = key.read_bytes()

        except Exception as ex:
            logging.error("Can't read from %s: %s", key, ex)
            return

        self.write(key, value)

    def rollback(self):
        for k, v in self.prev_values.items():
            self.write(k, v, False)


class DeviceMatchingInfo:
    ATTRS = ['device', 'vbios_version']
    PROPS = ['PCI_ID', 'PCI_SLOT_NAME', 'PCI_SUBSYS_ID']

    def __init__(self, device):
        self.data = {
            p.lower(): device.properties.get(p) for p in self.PROPS
        }
        self.data.update({
            a: device.attributes.get(a) for a in self.ATTRS
        })

    def match(self, config):
        score = 0

        for key, value in self.data.items():
            config_value = getattr(config, key)

            if config_value is None:
                continue

            if value != config_value:
                return -1

            score += 1

        return score

    def best_match(self, configs):
        best_config, best_score = None, -1

        for config in configs:
            score = self.match(config)

            if score > best_score:
                best_config, best_score = config, score

        return best_config, best_score


async def update_loop(configs, udev):
    fan_controllers = []
    sysfs = SysfsOverride()

    for device in udev.list_devices(DRIVER='amdgpu'):
        device_info = DeviceMatchingInfo(device)
        logging.info("Identification data for %r: %r", device, device_info.data)
        config, score = device_info.best_match(configs)
        if config is None:
            continue

        logging.info("Matched config %r to %r (score %d)", config.name, device, score)

        hwmon_devices = list(udev.list_devices(subsystem='hwmon', parent=device))
        if len(hwmon_devices) == 1:
            hwmon_path = pathlib.Path(hwmon_devices[0].sys_path)

            if config.fan_control:
                try:
                    fan_controllers.append(FanController(hwmon_path, config))
                except Exception:
                    logging.exception("Failed to enable fan control for device %s", hwmon_path)

            if config.power_cap is not None:
                sysfs[hwmon_path / 'power1_cap'] = str(config.power_cap)

        elif len(hwmon_devices) > 1:
            logging.warning("Device %r has %d hwmon devices, don't know how to handle that", device, len(hwmon_devices))

    try:
        while True:
            if fan_controllers:
                await asyncio.sleep(1)

                for fan in fan_controllers:
                    try:
                        fan.update()
                    except Exception:
                        logging.exception("Failed to update device %s", fan.sysfs_path)
            else:
                await asyncio.get_event_loop().create_future()

    finally:
        for fan in fan_controllers:
            try:
                fan.restore_pwm_enable()
            except Exception:
                logging.exception("Failed to restore device %s", fan.sysfs_path)

        try:
            sysfs.rollback()
        except Exception:
            logging.exception("Failed to roll back sysfs changes")


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


def parse_device_config(section):
    parsed = {}

    for option, option_type in DEVICE_CONFIG_FIELDS.items():
        try:
            if option_type == bool:
                parsed[option] = section.getboolean(option, DEVICE_CONFIG_DEFAULTS.get(option))
            else:
                value = section.get(option, DEVICE_CONFIG_DEFAULTS.get(option))
                if value is not None:
                    value = option_type(value)

                parsed[option] = value

        except ValueError as ex:
            raise ValueError("%r in %r: %s", option, section.name, ex) from ex

    for option in section:
        if option not in parsed:
            logging.warning("Unknown option %r in section %r (known options: %r)", option, section.name, parsed.keys())

    return DeviceConfig(name=section.name, **parsed)


def interactive_confirm(prompt):
    while True:
        response = input(prompt + ' (y/n) ').lower()
        if response == 'y':
            return True
        if response == 'n':
            return False
        print("Response {} is not recognized. Please type 'y' or 'n'.")


def run_if_found(args):
    cmd = shutil.which(args[0])
    pretty_cmd = ' '.join(shlex.quote(a) for a in args)
    if cmd:
        if interactive_confirm("{!r} found at {}. Run {!r} automatically?".format(args[0], cmd, pretty_cmd)):
            subprocess.check_call([cmd] + args[1:])
            return True

    return False


def overdrive_unlock():
    modconf_path = pathlib.Path('/etc/modprobe.d/amdgpu-overdrive.conf')
    modconf_data = 'options amdgpu ppfeaturemask={}'.format(ppfeaturemask | PP_OVERDRIVE_MASK)
    if modconf_path.exists():
        modconf_current = modconf_path.read_text()
    else:
        modconf_current = None

    ppfeaturemask = int(ppfeaturemask_path.read_bytes())
    if (PP_OVERDRIVE_MASK & ppfeaturemask) or (modconf_current == modconf_data):
        print("Overdrive is already enabled")
        return

    modconf_path.write_text(modconf_data)

    print("{} has been written. Please update your initramfs and reboot.".format(modconf_path))

    if run_if_found(['update-initramfs', '-u']):
        return

    if run_if_found(['mkinitcpio', '-P']):
        return

    if run_if_found(['dracut', '--regenerate-all', '-f']):
        return


def main(*args, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=pathlib.Path)
    arg = parser.parse_args(*args, **kwargs)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = configparser.ConfigParser()
    config.read(arg.config)
    parsed_sections = [parse_device_config(s) for s in config.values()]
    logging.info("Configs: %r", parsed_sections)

    main_loop = asyncio.get_event_loop()
    main_task = main_loop.create_task(main_async(parsed_sections))

    main_loop.add_signal_handler(signal.SIGINT, main_task.cancel)
    main_loop.add_signal_handler(signal.SIGTERM, main_task.cancel)

    try:
        main_loop.run_until_complete(main_task)

    except asyncio.CancelledError:
        pass


if __name__ == '__main__':
    main()
