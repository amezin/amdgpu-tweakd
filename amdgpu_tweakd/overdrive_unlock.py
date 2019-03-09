import pathlib
import shlex
import shutil
import subprocess


PP_OVERDRIVE_MASK = 0x4000
ppfeaturemask_path = pathlib.Path('/sys/module/amdgpu/parameters/ppfeaturemask')


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


def update_initramfs():
    if run_if_found(['update-initramfs', '-u']):
        return

    if run_if_found(['mkinitcpio', '-P']):
        return

    if run_if_found(['dracut', '--regenerate-all', '-f']):
        return


def main():
    ppfeaturemask = int(ppfeaturemask_path.read_bytes())

    modconf_path = pathlib.Path('/etc/modprobe.d/amdgpu-overdrive.conf')
    modconf_data = 'options amdgpu ppfeaturemask={}'.format(ppfeaturemask | PP_OVERDRIVE_MASK)
    if modconf_path.exists():
        modconf_current = modconf_path.read_text()
    else:
        modconf_current = None

    if modconf_current == modconf_data:
        print("Overdrive is already enabled")

        if not (PP_OVERDRIVE_MASK & ppfeaturemask):
            print("Please update your initramfs and reboot")
            update_initramfs()

        return

    modconf_path.write_text(modconf_data)

    print("{} has been written. Please update your initramfs and reboot.".format(modconf_path))

    update_initramfs()


if __name__ == '__main__':
    main()
