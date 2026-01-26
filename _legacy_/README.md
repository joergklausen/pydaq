# pydaq
Data acquisition and instrument control for selected instrumentation in use at GAW and/or AQ stations.

# Setup target system
## Raspberry
### OS
source: https://www.tomshardware.com/reviews/raspberry-pi-headless-setup-how-to,6028.html
1. Download RPI imager from https://www.raspberrypi.com/software/ and install OS.
2. Click Edit Settings from the pop-up.
3. Fill in all the fields on the General tab: hostname, username / password, wireless LAN (if you plan to use wifi), and locale settings. Make sure to choose timezone=UTC.
4. On the Services tab, toggle enable SSH to on and select "Use password authentication."  Then click Save.
5. Click Yes to apply OS customization settings.
6. Pop SD card into the Pi and power it up. It should connect to the LAN using wifi. To verify, we need to open an SSH connection. For ease of use, we will also use Raspberry Connect.

### Raspberry Connect
1. Open a terminal
2. $ ssh <ip>
3. $ sudo apt update
4. $ sudo apt install --upgrade ...

### Setup git and VS Code
1. $ sudo apt-get install git -y
2. $ sudo apt-get install code

# setup crontab for automatic execution like so ...
$ crontab -e

1. add the code shown in file 'cron'
2. make executable
$ sudo chmod +x /home/gaw/git/nrbdaq/nrbdaq.sh

# Alternatively, set up a systemd service
1. copy file 'nrbdaq.service' to /etc/systemd/system/nrbdaq.service
2. make executable
$ sudo chmod 744 /home/gaw/git/nrbdaq/nrbdaq.sh
$ sudo chmod 664 /etc/systemd/system/nrbdaq.service
3. enable service
$ sudo systemctl daemon-reload
$ sudo systemctl enable nrbdaq.service

# linux goodies
## read journal
$ sudo journalctl -p err --since "2024-08-27" --until "2024-08-29"
    show all journal entries of level ERROR in specified period

$ systemctl status cron.service
    show status of cron

$ ps [aux]
    show active processes
    [a: displays information about other users' processes as well as your own.
     u: displays the processes belonging to the specified usernames.
     x: includes processes that do not have a controlling terminal.]

## list active instances of nrbdaq.py
$ pgrep -f -a "nrbdaq.py"

## kill a process by id
$ kill <pid>

## list USB / serial ports
$ dmesg | grep tty
## How-to operate Get red-y MFCs
1. Install cable PPDM-U driver from /resources
2. Install get red-y MFC software
3. Plug in USB cable, check COM port used in device manager
4. Start get red-y software, select port, search for device, stop search once found

## Setup
2024-10-22/jkl
- Aurora3000
    - flow controlled by get red-y MFC set to 4 lnpm
    - dark count found to be 450-500
    - Wavelength 1 Shtr Count ca 1.1M
    - Wavelength 2 Shtr Count ca 1.6M
    - Wavelength 3 Shtr Count ca 1.7M
- AE31 flow controlled by get red-y set to 3 lnpm
    -


## FIDAS
### SOP
Each station visit:
- Copy data from USB stick to central Minix (../<user>/Documents/pydaq/data/Fidas)
