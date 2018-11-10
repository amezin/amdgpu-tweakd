import distutils.core
import distutils.util
import os.path
import setuptools.command.install
import subprocess
import sys


class InstallSystemdUnit(distutils.core.Command):
    description = "install systemd unit file"

    user_options = [
        ('install-systemd-unit=', None,
         "systemd unit installation directory"),
        ('root=', None,
         "install everything relative to this alternate root directory"),
        ('force', 'f', "force installation (overwrite existing files)"),
    ]

    boolean_options = ['force']

    unit_file = 'radeon-fan-control.service'

    def initialize_options(self):
        self.outfiles = []
        self.install_systemd_unit = None
        self.root = None
        self.force = 0

    def finalize_options(self):
        self.set_undefined_options('install',
                                   ('root', 'root'),
                                   ('force', 'force'))

        if self.install_systemd_unit is None:
            self.install_systemd_unit = self.detect_systemd_dir()

    def run(self):
        if not self.install_systemd_unit:
            self.warn("Skipping systemd unit installation")
            return

        self.mkpath(self.install_systemd_unit)

        out_file = os.path.join(self.install_systemd_unit,
                                os.path.basename(self.unit_file))
        self.make_file([self.unit_file], out_file,
                       self.write_unit_file, (self.unit_file, out_file))
        self.outfiles.append(out_file)

    def detect_systemd_dir(self):
        try:
            system_unit_dir = subprocess.check_output(
                ['pkg-config', '--variable=systemdsystemunitdir', 'systemd'],
                universal_newlines=True
            ).strip()

        except subprocess.CalledProcessError as ex:
            self.warn("Can't detect systemd system unit dir: " + str(ex))
            return

        if self.root is None:
            return systemd_unit_dir

        return distutils.util.change_root(self.root, system_unit_dir)

    def write_unit_file(self, template_path, out_path):
        with open(template_path, 'rt') as template_file:
            template = template_file.read()

        install_scripts = self.get_finalized_command("install_scripts").install_dir
        if self.root is not None and install_scripts.startswith(self.root):
            install_scripts = '/' + install_scripts[len(self.root):]

        template = template.format(install_scripts=install_scripts)

        with open(out_path, 'wt') as out_file:
            out_file.write(template)

    def get_inputs(self):
        return [self.unit_file]

    def get_outputs(self):
        return self.outfiles


class Install(setuptools.command.install.install):
    sub_commands = setuptools.command.install.install.sub_commands + [('install_systemd_unit', lambda _: True)]


setuptools.setup(
    name='radeon-fan-control',
    py_modules=['radeon_fan_control'],
    entry_points = {
        'console_scripts': ['radeon-fan-control=radeon_fan_control:main'],
    },
    install_requires = ['jeepney>=0.4'],
    cmdclass = {
        'install_systemd_unit': InstallSystemdUnit,
        'install': Install
    }
)
