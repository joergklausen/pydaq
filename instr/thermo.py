"""
Concrete Thermo49i instrument class derived from the Instrument abstract base class.

This class supports both serial and TCP/IP communication with a Thermo 49i ozone analyzer.
"""

import logging
import random
import socket
import time

from datetime import datetime
from pathlib import Path

import colorama

from instr.instrument import Instrument


class Thermo49i(Instrument):
    """
    Implementation of a Thermo 49i ozone analyzer for automated data acquisition.
    """

    def __init__(self, name: str, config_path: str):
        """
        Initialize the Thermo49i instrument using configuration.

        Args:
            name (str): Instrument name (must match the name in config).
            config_path (str): Path to the YAML configuration file.
        """
        super().__init__(name, config_path)
        colorama.init(autoreset=True)
        self.logger = logging.getLogger(f"Thermo49i:{self._name}")

        self._get_config = self._instr["get_config"]
        self._set_config = self._instr["set_config"]
        self._get_data = self._instr["get_data"]
        self._averaging_interval = self._instr["averaging_interval"]
        self._reporting_interval = self._instr["reporting_interval"]

        # self.data_path = Path(self._instr["data_path"]).expanduser()
        # self.staging_path = Path(self._instr["staging_path"]).expanduser()
        # self.data_path.mkdir(parents=True, exist_ok=True)
        # self.staging_path.mkdir(parents=True, exist_ok=True)

        # set fixed configuration parameters
        self.send_command("set mode remote")
        self.send_command("set gas unit ppb")
        self.send_command("set temp comp on")
        self.send_command("set pres comp on")
        self.send_command("set range 1")
        self.send_command("set format 00")
        self.send_command("set lrec format 0")    # ASCII no labels
        self.send_command(f"set lrec per {self._averaging_interval}")

        # set variable configuration parameters
        if self._set_config:
            for cmd in self._set_config:
                response = self.send_command(cmd)
                if "ok" not in response.lower():
                    self.logger.warning(f"Command '{cmd}' returned unexpected response: {response}")

        # persist the configuration
        self.send_command("set save params")

        self._header = "pcdate pctime time date flags o3 hio3 cellai cellbi bncht lmpt o3lt flowa flowb pres"

        self._data = str()
        self._saved_data_path = Path()


    def _serial_comm(self, cmd: str) -> str:
        """Low-level serial command execution."""
        self._serial.open()
        self._serial.write(bytes([self._id]) + (f"{cmd}\x0D").encode())
        time.sleep(0.5)
        rcvd = b""
        while self._serial.in_waiting > 0:
            rcvd += self._serial.read(1024)
        self._serial.close()
        return rcvd.decode().split("*")[0].replace(cmd, "").strip()


    def _socket_comm(self, cmd: str) -> str:
        if self._sockmode == "tcp":
            return self._tcp_comm(cmd)
        else:
            raise NotImplementedError("TCP/IP is the only supported socket protocol for Thermo instruments.")


    def _tcp_comm(self, cmd: str) -> str:
        """Low-level TCP/IP command execution."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self._socktout)
            s.connect(self._sockaddr)
            s.sendall(bytes([self._id]) + (f"{cmd}\x0D").encode())
            time.sleep(self._socksleep)
            rcvd = b""
            while True:
                data = s.recv(1024)
                rcvd += data
                if b'\x00' in data:
                    break
        return rcvd.decode().split("*")[0].replace(cmd, "").strip()


    def get_data(self) -> str:
        """
        Read measurement data from instrument and add timestamp.

        Returns:
            str: Timestamped response string.
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        result = self.send_command(self._get_data)
        self._data += f"{now} {result}\n"
        return result


    def accumulate_data(self, data: str):
        """
        Append data to internal buffer.

        Args:
            data (str): Line of instrument output.
        """
        self._data += data + "\n"


    def get_all_lrec(self, save: bool=True) -> str:
        """download entire buffer from instrument and save to file

        :param bln save: Should data be saved to file? default=True
        :return str response as decoded string
        """
        try:
            data = str()

            # get current lrec format, then set lrec format
            cmd = "lrec format"
            lrec_format = self.send_command(cmd)
            _ = self.send_command(f"set {cmd} 0")
            if not 'ok' in _:
                self.logger.warning(f"'set {cmd} 0' returned '{_}' instead of 'ok'.")

            # retrieve numbers of lrec stored in buffer
            cmd = "no of lrec"
            no_of_lrec = int(self.send_command(cmd).split()[0])

            # retrieve all lrec records stored in buffer
            index = no_of_lrec
            batch_size = 10

            while index > 0:
                if index < 10:
                    batch_size = index
                cmd = f"lrec {str(index)} {str(batch_size)}"
                self.logger.info(cmd)
                data += f"{self.send_command(cmd)}\n"

                # remove all the extra info in the string returned
                # 05:26 07-19-22 flags 0C100400 o3 30.781 hio3 0.000 cellai 50927 cellbi 51732 bncht 29.9 lmpt 53.1 o3lt 0.0 flowa 0.435 flowb 0.000 pres 493.7
                # data = data.replace("flags ", "")
                # data = data.replace("hio3 ", "")
                # data = data.replace("cellai ", "")
                # data = data.replace("cellbi ", "")
                # data = data.replace("bncht ", "")
                # data = data.replace("lmpt ", "")
                # data = data.replace("o3lt ", "")
                # data = data.replace("flowa ", "")
                # data = data.replace("flowb ", "")
                # data = data.replace("pres ", "")
                # data = data.replace("o3 ", "")

                index = index - batch_size

            if save:
                self.save_data_file()
                self.stage_data_file()

            # restore lrec format
            _ = self.send_command(f'set {lrec_format}')
            if not 'ok' in _:
                self.logger.warning(f"'set {lrec_format}' returned '{_}' instead of 'ok'.")

            return data

        except Exception as err:
            self.logger.error(err)
            return str()


    def get_o3(self) -> str:
        try:
            if self.simulate:
                return str(random.randint(0, 1000) / 10)  # Simulate a random response
            else:
                if self._instr_comms:
                    return self._serial_comm('o3')
                else:
                    return self._socket_comm('o3')

        except Exception as err:
            self.logger.error(err)
            return str()


    def print_o3(self) -> None:
        try:
            if self._instr_comms:
                o3 = self._serial_comm('o3').split()
            else:
                o3 = self._socket_comm('o3').split()
            self.logger.info(colorama.Fore.GREEN + f"{self._name}, {o3[0].upper()} {str(float(o3[1]))} {o3[2]}")

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")


    def set_o3(self, level: str) -> str:
        try:
            if self.simulate:
                return level
            else:
                if self._instr_comms:
                    return self._serial_comm(f"set o3 {level}")
                else:
                    return self._socket_comm(f"set o3 {level}")

        except Exception as err:
            self.logger.error(err)
            return str()


    # def stage_data_file(self, data=None, destination=None):
    #     """
    #     Create a zip archive from the last saved file and move it to staging.
    #     """
    #     try:
    #         self.save_data()
    #         if self._saved_data_path is not None:
    #             archive = self.staging_path / self._saved_data_path.name.replace(".dat", ".zip")
    #             with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
    #                 zf.write(str(self._saved_data_path), arcname=self._saved_data_path.name)
    #             self.logger.info(f"Staged file to {archive}")
    #         else:
    #             self.logger.error("No saved data file to stage.")
    #     except Exception as e:
    #         self.logger.error(f"Failed to stage file: {e}")

    def set_datetime(self):
        """
        Set the instrument clock to system date and time.
        """
        try:
            date_cmd = f"set date {time.strftime('%m-%d-%y')}"
            time_cmd = f"set time {time.strftime('%H:%M:%S')}"

            date_result = self.send_command(date_cmd)
            self.logger.info(f"{self._name}, Date set to: {date_result}")
            time_result = self.send_command(time_cmd)
            self.logger.info(f"{self._name}, Time set to: {time_result}")

        except Exception as err:
            self.logger.error(err)

    def get_config(self) -> dict:
        """
        Read configuration settings from the instrument.

        Returns:
            dict: Mapping of command to returned value.
        """
        cfg = {}
        try:
            for cmd in self._get_config:
                cfg[cmd] = self.send_command(cmd)
            self.logger.info(f"{self._name}, Configuration read as: {cfg}")
            return cfg
        except Exception as err:
            self.logger.error(err)
            return {}

    def set_config(self) -> dict:
        """
        Send configuration settings to the instrument.

        Returns:
            dict: Mapping of command to response.
        """
        self.logger.info(f"{self._name}, Setting configuration")
        cfg = {}
        try:
            for cmd in self._set_config:
                cfg[cmd] = self.send_command(cmd)
                time.sleep(1)
            self.logger.info(f"{self._name}, Configuration set: {cfg}")
            return cfg
        except Exception as err:
            self.logger.error(err)
            return {}
