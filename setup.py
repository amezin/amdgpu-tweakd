import setuptools

setuptools.setup(
    name='radeon-fan-control',
    py_modules=['radeon_fan_control'],
    entry_points = {
        'console_scripts': ['radeon-fan-control=radeon_fan_control:main'],
    },
    data_files = [
        ('lib/systemd/system', ['radeon-fan-control.service'])
    ]
)
