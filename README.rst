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

Installation:
- Arch Linux: `PKGBUILD` is provided in `archlinux` folder
- Other Linux: TODO. But it's mostly a regular Python application, installable using `setup.py`

Configuration:
- Provided systemd unit (`amdgpu-tweakd.service`) expects configuration in `/etc/amdgpu-tweakd`. See `config.example`
- `# systemctl restart amdgpu-tweakd` to apply the configuration
- If you want to adjust the power limit, you may need to unlock overdrive first. Run `# amdgpu-unlock-overdrive` to do it.
