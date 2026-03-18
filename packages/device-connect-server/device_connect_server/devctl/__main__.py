"""Entry point for running devctl as a module.

Usage:
    python -m device_connect_server.devctl list
    python -m device_connect_server.devctl register --id myDevice
    python -m device_connect_server.devctl discover
    python -m device_connect_server.devctl commission <device_id> --pin 1234-5678
    python -m device_connect_server.devctl interactive
    python -m device_connect_server.devctl --help
"""

from device_connect_server.devctl.cli import main

if __name__ == "__main__":
    main()
