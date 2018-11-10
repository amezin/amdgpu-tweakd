import argparse
import asyncio
import configparser
import glob
import pathlib
import signal
import logging

import jeepney.bus_messages
import jeepney.integrate.asyncio


class HwmonDevice:
    def __init__(self, sysfs_path, config):
        config = dict(config)

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


def update_devices(devices, config):
    devices = { path: dev for path, dev in devices.items() if dev.sysfs_path.exists() }

    for section in config.sections():
        for resolved_path in glob.glob(section):
            if resolved_path not in devices:
                try:
                    devices[resolved_path] = HwmonDevice(resolved_path, config[section])
                except Exception as ex:
                    logging.exception("Failed to create device %s", resolved_path)

    for device in devices.values():
        try:
            device.update()
        except Exception as ex:
            logging.exception("Failed to update device %s", device.sysfs_path)

    return devices


async def update_loop(config):
    devices = {}

    try:
        while True:
            devices = update_devices(devices, config)
            await asyncio.sleep(1)

    finally:
        for dev in devices.values():
            dev.restore_pwm_enable()


async def main_async(config):
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
        update_loop_task = asyncio.ensure_future(update_loop(config))
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
