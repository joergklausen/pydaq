# Consider the following shell commands as well:
# - lsusb  lists all ports in action
# - ls /dev/tty*   lists all ports

import serial.tools.list_ports

def list_serial_ports():
    ports = serial.tools.list_ports.comports()
    for port, desc, hwid in ports:
        print(f"{port}: {desc} [{hwid}]")

if __name__ == "__main__":
    list_serial_ports()
