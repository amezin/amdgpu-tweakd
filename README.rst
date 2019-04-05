amdgpu-tweakd
-------------

Fan speed control & settings daemon for amdgpu on Linux. Uses hwmon interface.

Currently implemented:

- Software fan speed control based on GPU temperature
- Automatically turning the fan off
- Multi-GPU and multi-profile support. Profiles can be selected by PCI ids or vbios version (see config.example)
- A script that semi-automatically enables overclocking (requires a reboot though)
- Power limit setting
- All changes are automatically rolled back when the daemon stops
- All settings are automatically reapplied when the system resumes from sleep/hibernation
