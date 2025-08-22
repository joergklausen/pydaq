"""
Abstract instrument base class for implementing data acquisition drivers.

Each concrete instrument implementation should subclass this and define methods
for reading, accumulating, and writing data. Configuration is loaded from a
YAML file using utilities provided in `utils.config_utils`.
"""

import logging
import random  # Used for simulation purposes in tests
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Optional

import schedule
import serial

from utils.config import load_yaml_config

class Instrument(ABC):
    """
    Abstract base class for instruments.
    Specific instrument classes should implement the required methods.
    Interfaces can use TCP/IP or serial ports.
    """

    def __init__(self, name: str, config_path: str):
        """
        Initialize an instrument with a name and configuration loaded from a YAML file.

        Args:
            name (str): Name of the instrument.
            config_path (str): Path to the YAML configuration file.
        """
        self._name = name
        self.logger = logging.getLogger(f"Instrument:{self._name}")

        # Load general and instrument configuration from the provided YAML file.
        self._general = load_yaml_config(config_path)
        if not self._general:
            raise ValueError(f"Configuration file {config_path} not found or invalid.")

        self._instr = self._general.get("instruments", {}).get(name, {})
        if not self._instr:
            raise ValueError(f"Instrument '{name}' not found in configuration.")

        # Configure local paths
        self.data_path = (
            Path(self._general.get("paths", {}).get("data", "data")) / self._name
            ).expanduser().resolve()
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.staging_path = (
            Path(self._general.get("paths", {}).get("staging", "staging")) / self._name
            ).expanduser().resolve()
        self.staging_path.mkdir(parents=True, exist_ok=True)
        self._staging_zip = self._instr.get("staging_zip", True)

        # Instrument metadata
        self._id = self._instr.get("id", "ID unknown")
        self._serial_number = self._instr.get("serial_number", "S/N unknown")
        self.simulate = self._instr.get("simulate", False)

        # Configure instrument communication
        self._instr_comms = self._instr.get("communication", "").lower()
        if not self._instr_comms in ("serial", "socket"):
            raise ValueError("Instrument communication must be 'serial' or 'socket'. Verify config.")

        if self._instr_comms=="serial":
            port = self._instr.get("serial", "COM1")
            port_cfg = self._general.get("ports", {}).get(port, {
                "baudrate": 9600,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
                "timeout": 2    # seconds
            })
            self._serial = serial.Serial(
                port=port,
                baudrate=port_cfg["baudrate"],
                bytesize=port_cfg["bytesize"],
                parity=port_cfg["parity"],
                stopbits=port_cfg["stopbits"],
                timeout=port_cfg["timeout"]
            )
            if self._serial.is_open:
                self._serial.close()
        else:
            sock = self._instr["socket"]
            self._sockmode = sock.get("mode", "tcp").lower()
            self._sockaddr = (sock["host"], sock["port"])
            self._socktout = sock["timeout"]
            self._socksleep = sock["sleep"]

        # Configure data logging
        self._data: str = ""
        self._saved_data_path: Optional[Path] = None
        self._header: str = ""
        self._averaging_interval = self._instr.get("averaging_interval", 1)
        self._reporting_interval = self._instr.get("reporting_interval", 60)
        if self._reporting_interval >= 1440:
            self._file_timestamp_format = "%Y%m%d"
        elif self._reporting_interval >= 60:
            self._file_timestamp_format = "%Y%m%d%H"
        else:
            self._file_timestamp_format = "%Y%m%d%H%M"

        # Configure data transfer
        self._remote_protocol = self._general.get("remote", {}).get("protocol", "").lower()
        if self._remote_protocol == "sftp":
            self.remote_path = Path(self._general.get("remote", {}).get("sftp", {}).get("usr", ""))
        elif self._remote_protocol == "mqtt":
            self.remote_path = Path(self._general.get("remote", {}).get("mqtt", {}).get("topic", ""))
        else:
            self.remote_path = Path()


    @property
    def name(self) -> str:
        """Return the instrument name."""
        return self._name

    @property
    def instrument_config(self) -> dict:
        """Return the instrument-specific configuration dictionary."""
        return self._instr


    def send_command(self, cmd: str) -> str:
        """
        Send a command and receive the response (if any) via serial or TCP/IP.

        Args:
            cmd (str): Command string to send.

        Returns:
            str: Decoded response string.
        """
        try:
            if self.simulate:
                return str(random.randint(0, 1000) / 10)  # Simulate a random response
            if self._instr_comms=="serial":
                return self._serial_comm(cmd)
            else:
                return self._socket_comm(cmd)
        except Exception as err:
            self.logger.error(f"Communication error: {err}")
            return ""


    def save_data_file(self) -> None:
        """
        Save accumulated measurement data to a timestamped .dat file.

        The file is written to `self.data_path`, with a header if it does not yet exist.
        The file name includes the instrument name and a formatted timestamp.
        """
        try:
            if not self._data.strip():
                self.logger.warning("No data to save.")
                return

            timestamp = datetime.now().strftime(self._file_timestamp_format)
            filename = f"{self._name}-{timestamp}.dat"
            data_file_path = self.data_path / filename

            # Write data to file (append if it exists)
            mode = "a" if data_file_path.exists() else "w"
            with open(data_file_path, mode) as fh:
                if mode == "w":
                    fh.write(self._header + "\n")
                fh.write(self._data)

            self.logger.info(f"Saved data to {data_file_path}")
            self._saved_data_path = data_file_path

            # Clear the accumulated data after saving
            self._data = ""

        except Exception as err:
            self.logger.error("Failed to save data: %s", err)


    def stage_data_file(self) -> None:
        """
        Stage the most recently saved data file to the staging directory.

        The staging path is derived from the config. If it does not exist, it is created.
        """
        try:
            if not self._saved_data_path or not self._saved_data_path.exists():
                self.logger.warning("No saved data file to stage.")
                return

            source = self._saved_data_path
            target = self.staging_path / self._saved_data_path.name

            if source.exists():
                if self._staging_zip:
                    with zipfile.ZipFile(target.with_suffix('.zip'), 'w') as zf:
                        zf.write(source, arcname=source.name)
                    self.logger.info(f"Staged {source} to {target.with_suffix('.zip')}")
                else:
                    # Copy the file directly if not zipping
                    target.write_bytes(source.read_bytes())
                self.logger.info(f"Staged {source} → {target}")
            else:
                self.logger.warning(f"No data file found to stage: {source}")

        except Exception as err:
            self.logger.error("Failed to stage data file: %s", err)


    def transfer_files(self) -> bool:
        """
        Transfer staged data using the globally configured transfer protocol.

        Returns:
            bool: True if transfer succeeded, False otherwise.
        """
        protocol = self._general.get("transfer_protocol", "").lower()

        if protocol == "sftp":
            try:
                sftp = import_module("utils.sftp")
                client = sftp.SFTPClient(self._general)

                client.transfer_files(
                    local_path=str(self.staging_path),
                    remote_path=self._general["sftp"]["remote_path"],
                    remove_on_success=True
                )
                return True
            except Exception as err:
                self.logger.error(f"SFTP transfer failed: {err}")
                return False
        elif protocol == "mqtt":
            try:
                mqtt = import_module("utils.mqtt")
                instr_topic = self._instr.get("mqtt_topic")
                if not instr_topic:
                    raise ValueError("Missing 'mqtt_topic' in instrument config.")

                client = mqtt.MQTTClient(self._general, instr_topic)
                client.transfer_files(
                    local_path=str(self.staging_path),
                    remove_on_success=True
                )
                return True
            except Exception as err:
                self.logger.error(f"MQTT transfer failed: {err}")
                return False

        else:
            self.logger.error(f"Transfer protocol '{protocol}' is not supported.")
            return False


    def setup_schedules(self) -> bool:
        """
        Set up acquisition and file staging intervals using `schedule`.

        Returns:
            bool: True if schedules are successfully set.
        """
        try:
            schedule.every(self._averaging_interval).minutes.at(":00").do(self.get_data)
            schedule.every(self._averaging_interval).minutes.at(":00").do(self.save_data_file)

            if self._reporting_interval == 10:
                self._file_timestamp_format = "%Y%m%d%H%M"
                for minute in ["00", "10", "20", "30", "40", "50"]:
                    schedule.every().hour.at(f"{minute}:01").do(self.stage_data_file)
            elif self._reporting_interval == 1440:
                self._file_timestamp_format = "%Y%m%d"
                schedule.every().day.at("00:00:01").do(self.stage_data_file)
            else:
                self._file_timestamp_format = "%Y%m%d%H"
                schedule.every().hour.at("00:01").do(self.stage_data_file)

            return True
        except Exception as err:
            self.logger.error(err)
            return False


    @abstractmethod
    def get_data(self) -> str:
        """
        Read (instant) measurement data from the instrument.

        Returns:
            str: Measurement data from instrument.
        """
        pass

    @abstractmethod
    def accumulate_data(self, data: str):
        """
        Accumulate data from multiple reads into internal buffer.

        Args:
            data (str): Data string to accumulate.
        """
        pass

    @abstractmethod
    def _serial_comm(self, cmd: str) -> str:
        """
        Send a command over a serial connection.

        Args:
            cmd (str): Command string to send.

        Returns:
            str: Response from the instrument.
        """
        pass

    @abstractmethod
    def _socket_comm(self, cmd: str) -> str:
        """
        Send a command over a socket connection.

        Args:
            cmd (str): Command string to send.

        Returns:
            str: Response from the instrument.
        """
        pass

    @abstractmethod
    def set_datetime(self):
        """
        Set the instrument's internal clock to the current system date and time.
        """
        pass

    @abstractmethod
    def get_config(self) -> dict:
        """
        Retrieve the instrument's current configuration.

        Returns:
            dict: Key-value pairs of configuration parameters.
        """
        pass

    @abstractmethod
    def set_config(self) -> dict:
        """
        Set the instrument's configuration from internal settings.

        Returns:
            dict: Key-value pairs of set commands and their responses.
        """
        pass
