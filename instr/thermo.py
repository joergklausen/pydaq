"""
instr/thermo.py

Define a class Thermo(Instrument) facilitating communication with Thermo 49C and 49i ozone analyzers.

@author: joerg.klausen@meteoswiss.ch
"""

from __future__ import annotations

import random
import socket
import time
from datetime import datetime

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None

from instr.instrument import Instrument, with_serial


class Thermo(Instrument):
    """Unified Thermo ozone analyzer driver (Thermo 49C / 49i)."""

    def __init__(self, name: str, config_path: str):
        super().__init__(name, config_path)

        self._get_cfg_cmds = list(self._params.get("get_config", []))

        # configure instrument configuration commands
        self._set_cfg_cmds = ["set mode remote"]
        self._set_cfg_cmds += list(self._params.get("set_config", []))

        # - set avg time tt  based on sampling_interval_seconds
        set_avg_time_dict = dict(zip((10, 20, 30, 60, 90, 120, 180, 240, 300), range(9)))
        sampling_interval_seconds = self._params.get("sampling_interval_seconds", 60)
        if sampling_interval_seconds in set_avg_time_dict.keys():
            tt = set_avg_time_dict[sampling_interval_seconds]
        else:
            raise ValueError(f"sampling_interval_seconds must be one of {set_avg_time_dict.keys()}.")
        self._set_cfg_cmds.append(f"set avg time {tt}")
        
        # verify communication protocol setup
        model = str(self._params.get("model", "49i")).upper()
        if model not in {"49C", "49I"}:
            self.logger.warning("Unknown model '%s'; defaulting to 49i behavior.", model)
            model = "49I"
        self._model = model
        if self._model == "49C" and self._params_comms == "socket":
            self.logger.warning("Model 49C does not support sockets; falling back to serial.")
            self._params_comms = "serial"

        # set lrec format based on model
        if self._model == "49I":
            self._get_data_cmd = self._params.get("get_data", "lr00")
            
            # - set lrec per value        # based on sampling_interval_seconds
            set_lrec_per_dict = dict(zip((60, 300, 900, 1800, 3600), (1, 5, 15, 30, 60)))
            if sampling_interval_seconds in set_lrec_per_dict.keys():
                value = set_lrec_per_dict[sampling_interval_seconds]
                self._set_cfg_cmds.append(f"set lrec per {value}")
                self._set_cfg_cmds.append(f"set lrec format 0")
            else:
                raise ValueError(f"sampling_interval_seconds must be one of {set_lrec_per_dict.keys()}.")
        if self._model == "49C":
            self._get_data_cmd = self._params.get("get_data", "lrec")

            # - set lrec format tt 02  based on sampling_interval_seconds
            set_lrec_format_dict = dict(zip((60, 300, 900, 1800, 3600), ('00', '01', '02', '03', '04')))
            if sampling_interval_seconds in set_lrec_format_dict.keys():
                tt = set_lrec_format_dict[sampling_interval_seconds]
                self._set_cfg_cmds.append(f"set lrec format {tt} 02")
            else:
                raise ValueError(f"sampling_interval_seconds must be one of {set_lrec_format_dict.keys()}.")

        # save configuration in instrument (must be last)
        self._set_cfg_cmds.append(f"set save params")

        self._header = self._header or f"# {self._name} {self._model}  S/N={self._serial_number}  id={self._id}"

    @with_serial
    def _serial_comm(self, cmd: str) -> str:
        assert self._serial is not None
        payload = bytes([int(self._id) & 0xFF]) + (f"{cmd}\r").encode()
        try:
            try:
                self._serial.reset_input_buffer()
            except Exception:
                pass
            self._serial.write(payload)
            time.sleep(0.5)

            rcvd = bytearray()
            last_len = -1
            while True:
                chunk = self._serial.read(1024)
                if chunk:
                    rcvd += chunk
                if len(rcvd) == last_len:
                    break
                last_len = len(rcvd)
            return self._parse_reply(cmd, bytes(rcvd))
        except Exception as err:
            self.logger.error("serial_comm(%s) failed: %s", cmd, err)
            return ""

    def _socket_comm(self, cmd: str) -> str:
        if self._sockmode == "tcp":
            with socket.create_connection(self._sockaddr, timeout=self._socktout) as s:
                s.sendall((f"{cmd}\r").encode())
                s.settimeout(self._socktout)
                rcvd = b""
                t0 = time.time()
                while True:
                    try:
                        data = s.recv(1024)
                        if not data:
                            break
                        rcvd += data
                        if b"\x00" in data:
                            break
                    except socket.timeout:
                        break
                    if time.time() - t0 > self._socktout:
                        break
            return self._parse_reply(cmd, rcvd)

        elif self._sockmode == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(self._socktout)
                s.sendto((f"{cmd}\r").encode(), self._sockaddr)
                try:
                    rcvd, _ = s.recvfrom(4096)
                except socket.timeout:
                    rcvd = b""
            return self._parse_reply(cmd, rcvd)

        else:
            raise ValueError(f"Unsupported socket mode: {self._sockmode}")

    @staticmethod
    def _parse_reply(cmd: str, raw: bytes) -> str:
        txt = raw.decode(errors="ignore")
        if "*" in txt:
            txt = txt.split("*", 1)[0]
        return txt.replace(cmd, "").strip()

    def send_command(self, cmd: str) -> str:
        try:
            if self.simulate:
                if cmd.lower().startswith("set "):
                    return "OK"
                if cmd.lower() in {"o3", "lr00"}:
                    return f"{random.randint(0,1000)/10:.1f}"
                return "SIM"

            if self._params_comms == "serial":
                return self._serial_comm(cmd)
            else:
                if self._model == "49C":
                    raise RuntimeError("49C does not support socket communication.")
                return self._socket_comm(cmd)
        except Exception as err:
            self.logger.error("send_command(%s) failed: %s", cmd, err)
            return ""

    # abstract implementations
    def get_data(self) -> str:
        result = self.send_command(self._get_data_cmd)
        if result:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._data += f"{now} {result}\n"
        return result

    def accumulate_data(self, data: str):
        self._data += (data.rstrip("\n") + "\n")

    def set_datetime(self):
        """
        Set the instrument clock to system date and time.
        """
        try:
            date_cmd = f"set date {time.strftime('%m-%d-%y')}"
            time_cmd = f"set time {time.strftime('%H:%M:%S')}"

            date_result = self.send_command(date_cmd)
            time_result = self.send_command(time_cmd)
            self.logger.info(f"{self._name}, DateTime set to: {date_result} {time_result}", extra={"to_logfile": True})

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
            for cmd in self._get_cfg_cmds:
                cfg[cmd] = self.send_command(cmd)
            self.logger.info(f"{self._name}, Configuration read as: {cfg}", extra={"to_logfile": True})
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
        self.logger.info(f"{self._name}, Setting configuration", extra={"to_logfile": True})
        cfg = {}
        try:
            for cmd in self._set_cfg_cmds:
                cfg[cmd] = self.send_command(cmd)
                # time.sleep(1)
            self.logger.info(f"{self._name}, Configuration set: {cfg}")
            return cfg
        except Exception as err:
            self.logger.error(err)
            return {}


    def get_o3(self) -> str:
        return self.send_command("o3")

    def set_o3(self, level: str) -> str:
        return self.send_command(f"set o3 {level}")

    def print_o3(self) -> str:
        val = self.get_o3()
        return f"O3: {val}" if val else "O3: <no data>"

# # instr/thermo.py
# from __future__ import annotations

# import random
# import socket
# import time
# from datetime import datetime

# try:
#     import serial  # type: ignore
# except Exception:  # pragma: no cover
#     serial = None

# from instr.instrument import Instrument, with_serial


# class Thermo(Instrument):
#     """Unified Thermo ozone analyzer driver (Thermo 49C / 49i)."""

#     def __init__(self, name: str, config_path: str):
#         super().__init__(name, config_path)

#         model = str(self._params.get("model", "49i")).upper()
#         if model not in {"49C", "49I"}:
#             self.logger.warning("Unknown model '%s'; defaulting to 49i behavior.", model)
#             model = "49I"
#         self._model = model
#         if self._model == "49C" and self._params_comms == "socket":
#             self.logger.warning("Model 49C does not support sockets; falling back to serial.")
#             self._params_comms = "serial"

#         self._get_data_cmd = self._params.get("get_data", "lr00")
#         self._get_cfg_cmds = list(self._params.get("get_config", []))
#         self._set_cfg_cmds = list(self._params.get("set_config", []))

#         self._header = f"# {self._name} {self._model}  S/N={self._serial_number}  id={self._id}"

#     @with_serial
#     def _serial_comm(self, cmd: str) -> str:
#         assert self._serial is not None
#         payload = bytes([int(self._id) & 0xFF]) + (f"{cmd}\r").encode()
#         try:
#             try:
#                 self._serial.reset_input_buffer()
#             except Exception:
#                 pass
#             self._serial.write(payload)
#             time.sleep(0.5)

#             rcvd = bytearray()
#             last_len = -1
#             while True:
#                 chunk = self._serial.read(1024)
#                 if chunk:
#                     rcvd += chunk
#                 if len(rcvd) == last_len:
#                     break
#                 last_len = len(rcvd)
#             return self._parse_reply(cmd, bytes(rcvd))
#         except Exception as err:
#             self.logger.error("serial_comm(%s) failed: %s", cmd, err)
#             return ""

#     def _socket_comm(self, cmd: str) -> str:
#         if self._sockmode == "tcp":
#             with socket.create_connection(self._sockaddr, timeout=self._socktout) as s:
#                 s.sendall((f"{cmd}\r").encode())
#                 s.settimeout(self._socktout)
#                 rcvd = b""
#                 t0 = time.time()
#                 while True:
#                     try:
#                         data = s.recv(1024)
#                         if not data:
#                             break
#                         rcvd += data
#                         if b"\x00" in data:
#                             break
#                     except socket.timeout:
#                         break
#                     if time.time() - t0 > self._socktout:
#                         break
#             return self._parse_reply(cmd, rcvd)

#         elif self._sockmode == "udp":
#             with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
#                 s.settimeout(self._socktout)
#                 s.sendto((f"{cmd}\r").encode(), self._sockaddr)
#                 try:
#                     rcvd, _ = s.recvfrom(4096)
#                 except socket.timeout:
#                     rcvd = b""
#             return self._parse_reply(cmd, rcvd)

#         else:
#             raise ValueError(f"Unsupported socket mode: {self._sockmode}")

#     @staticmethod
#     def _parse_reply(cmd: str, raw: bytes) -> str:
#         txt = raw.decode(errors="ignore")
#         if "*" in txt:
#             txt = txt.split("*", 1)[0]
#         return txt.replace(cmd, "").strip()

#     def send_command(self, cmd: str) -> str:
#         try:
#             if self.simulate:
#                 if cmd.lower().startswith("set "):
#                     return "OK"
#                 if cmd.lower() in {"o3", "lr00"}:
#                     return f"{random.randint(0,1000)/10:.1f}"
#                 return "SIM"

#             if self._params_comms == "serial":
#                 return self._serial_comm(cmd)
#             else:
#                 if self._model == "49C":
#                     raise RuntimeError("49C does not support socket communication.")
#                 return self._socket_comm(cmd)
#         except Exception as err:
#             self.logger.error("send_command(%s) failed: %s", cmd, err)
#             return ""

#     # abstract implementations
#     def get_data(self) -> str:
#         result = self.send_command(self._get_data_cmd)
#         if result:
#             now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#             self._data += f"{now} {result}\n"
#         return result

#     def accumulate_data(self, data: str):
#         self._data += (data.rstrip("\n") + "\n")

#     def set_datetime(self) -> None:
#         self.logger.info("set_datetime: not implemented for Thermo; skipping.")

#     def get_config(self) -> dict:
#         out = {}
#         for key in self._get_cfg_cmds:
#             out[key] = self.send_command(key)
#         return out

#     def set_config(self) -> dict:
#         out = {}
#         for cmd in self._set_cfg_cmds:
#             out[cmd] = self.send_command(cmd)
#         return out

#     def get_o3(self) -> str:
#         return self.send_command("o3")

#     def set_o3(self, level: str) -> str:
#         return self.send_command(f"set o3 {level}")

#     def print_o3(self) -> str:
#         val = self.get_o3()
#         return f"O3: {val}" if val else "O3: <no data>"

# # instr/thermo.py
# from __future__ import annotations

# import random
# import socket
# import time
# from datetime import datetime

# try:
#     import serial  # type: ignore
# except Exception:  # pragma: no cover
#     serial = None  # allow import without pyserial in CI

# from instr.instrument import Instrument


# class Thermo(Instrument):
#     """
#     Unified Thermo ozone analyzer driver (Thermo 49C / 49i).

#     - 49C: serial only
#     - 49i: serial OR TCP/UDP depending on config["instruments"][name]["communication"]
#     """

#     def __init__(self, name: str, config_path: str):
#         super().__init__(name, config_path)

#         # Normalize model
#         self._model = str(self._params.get("model", "49i")).upper()
#         if self._model not in {"49C", "49I"}:
#             self.logger.warning("Unknown model '%s'; defaulting to 49i behavior.", self._model)
#             self._model = "49I"

#         # Enforce 49C constraint
#         if self._model == "49C" and self._params_comms == "socket":
#             self.logger.warning("Model 49C does not support sockets; falling back to serial.")
#             self._params_comms = "serial"

#         # Commands from YAML (attached config uses lr00 for data)
#         self._get_data_cmd = self._params.get("get_data", "lr00")
#         self._get_cfg_cmds = list(self._params.get("get_config", []))
#         self._set_cfg_cmds = list(self._params.get("set_config", []))

#         # Header for saved files
#         self._header = f"# {self._name} {self._model}  S/N={self._serial_number}  id={self._id}"

#     # -------- low-level comms --------

#     def _serial_comm(self, cmd: str) -> str:
#         """
#         Send command over serial. Protocol: [id byte] + ASCII cmd + CR.
#         Replies may echo and contain '*checksum'; both are removed.
#         """
#         if serial is None:
#             raise RuntimeError("pyserial is not available; cannot use serial communication.")

#         # Lazy-open serial (Instrument prepared _serial_port/_serial_cfg)
#         if self._serial is None:
#             self._serial = serial.Serial(
#                 port=self._serial_port,
#                 baudrate=self._serial_cfg["baudrate"],
#                 bytesize=self._serial_cfg["bytesize"],
#                 parity=self._serial_cfg["parity"],
#                 stopbits=self._serial_cfg["stopbits"],
#                 timeout=self._serial_cfg["timeout"],
#             )
#         if not self._serial.is_open:
#             self._serial.open()

#         try:
#             payload = bytes([int(self._id) & 0xFF]) + (f"{cmd}\r").encode()
#             self._serial.reset_input_buffer()
#             self._serial.write(payload)
#             time.sleep(0.5)

#             rcvd = b""
#             last_len = -1
#             # read until the buffer stops growing (simple heuristic)
#             while True:
#                 chunk = self._serial.read(1024)
#                 if chunk:
#                     rcvd += chunk
#                 if len(rcvd) == last_len:
#                     break
#                 last_len = len(rcvd)
#             return self._parse_reply(cmd, rcvd)
#         finally:
#             try:
#                 self._serial.close()
#             except Exception:
#                 pass

#     def _socket_comm(self, cmd: str) -> str:
#         """
#         Send command via TCP/UDP and return reply (49i only).
#         """
#         if self._sockmode == "tcp":
#             with socket.create_connection(self._sockaddr, timeout=self._socktout) as s:
#                 s.sendall((f"{cmd}\r").encode())
#                 s.settimeout(self._socktout)
#                 rcvd = b""
#                 t0 = time.time()
#                 while True:
#                     try:
#                         data = s.recv(1024)
#                         if not data:
#                             break
#                         rcvd += data
#                         if b"\x00" in data:  # many instruments terminate with NUL
#                             break
#                     except socket.timeout:
#                         break
#                     if time.time() - t0 > self._socktout:
#                         break
#             return self._parse_reply(cmd, rcvd)

#         elif self._sockmode == "udp":
#             with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
#                 s.settimeout(self._socktout)
#                 s.sendto((f"{cmd}\r").encode(), self._sockaddr)
#                 try:
#                     rcvd, _ = s.recvfrom(4096)
#                 except socket.timeout:
#                     rcvd = b""
#             return self._parse_reply(cmd, rcvd)

#         else:
#             raise ValueError(f"Unsupported socket mode: {self._sockmode}")

#     @staticmethod
#     def _parse_reply(cmd: str, raw: bytes) -> str:
#         """
#         Remove echoed command and trailing '*...' (checksum/trailer).
#         """
#         txt = raw.decode(errors="ignore")
#         if "*" in txt:
#             txt = txt.split("*", 1)[0]
#         return txt.replace(cmd, "").strip()

#     def send_command(self, cmd: str) -> str:
#         """
#         Unified command sender honoring simulate/transport/model.
#         """
#         try:
#             if self.simulate:
#                 if cmd.lower().startswith("set "):
#                     return "OK"
#                 if cmd.lower() in {"o3", "lr00"}:
#                     # sample numeric-like payload
#                     return f"{random.randint(0,1000)/10:.1f}"
#                 return "SIM"

#             if self._params_comms == "serial":
#                 return self._serial_comm(cmd)
#             else:
#                 if self._model == "49C":
#                     raise RuntimeError("49C does not support socket communication.")
#                 return self._socket_comm(cmd)
#         except Exception as err:
#             self.logger.error("send_command(%s) failed: %s", cmd, err)
#             return ""

#     # -------- abstract implementations (Instrument) --------

#     def get_data(self) -> str:
#         """
#         Read a data line and append a timestamped line to the buffer.
#         Uses the configured command (attached config uses 'lr00').
#         """
#         result = self.send_command(self._get_data_cmd)
#         if result:
#             now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#             self._data += f"{now} {result}\n"
#         return result

#     def accumulate_data(self, data: str):
#         self._data += (data.rstrip("\n") + "\n")

#     def set_datetime(self):
#         """
#         Set the instrument clock to system date and time.
#         """
#         try:
#             date_cmd = f"set date {time.strftime('%m-%d-%y')}"
#             time_cmd = f"set time {time.strftime('%H:%M:%S')}"

#             date_result = self.send_command(date_cmd)
#             time_result = self.send_command(time_cmd)
#             self.logger.info(f"{self._name}, DateTime set to: {date_result} {time_result}", extra={"to_logfile": True})

#         except Exception as err:
#             self.logger.error(err)

#     def get_config(self) -> dict:
#         out = {}
#         for key in self._get_cfg_cmds:
#             out[key] = self.send_command(key)
#         return out

#     def set_config(self) -> dict:
#         out = {}
#         for cmd in self._set_cfg_cmds:
#             out[cmd] = self.send_command(cmd)
#         return out

#     # Convenience
#     def get_o3(self) -> str:
#         return self.send_command("o3")

#     def set_o3(self, level: str) -> str:
#         return self.send_command(f"set o3 {level}")

#     def print_o3(self) -> str:
#         val = self.get_o3()
#         return f"O3: {val}" if val else "O3: <no data>"



# # # class Thermo49i(Instrument):
# # #     """
# # #     Implementation of a Thermo 49i ozone analyzer for automated data acquisition.
# # #     """

# # #     def __init__(self, name: str, config_path: str):
# # #         """
# # #         Initialize the Thermo49i instrument using configuration.

# # #         Args:
# # #             name (str): Instrument name (must match the name in config).
# # #             config_path (str): Path to the YAML configuration file.
# # #         """
# # #         super().__init__(name, config_path)
# # #         colorama.init(autoreset=True)
# # #         self.logger = logging.getLogger(f"Thermo49i:{self._name}")

# # #         self._get_config = self._params["get_config"]
# # #         self._set_config = self._params["set_config"]
# # #         self._get_data = self._params["get_data"]
# # #         self._averaging_interval = self._params["averaging_interval"]
# # #         self._reporting_interval = self._params["reporting_interval"]

# # #         # self.data_path = Path(self._params["data_path"]).expanduser()
# # #         # self.staging_path = Path(self._params["staging_path"]).expanduser()
# # #         # self.data_path.mkdir(parents=True, exist_ok=True)
# # #         # self.staging_path.mkdir(parents=True, exist_ok=True)

# # #         # set fixed configuration parameters
# # #         self.send_command("set mode remote")
# # #         self.send_command("set gas unit ppb")
# # #         self.send_command("set temp comp on")
# # #         self.send_command("set pres comp on")
# # #         self.send_command("set range 1")
# # #         self.send_command("set format 00")
# # #         self.send_command("set lrec format 0")    # ASCII no labels
# # #         self.send_command(f"set lrec per {self._averaging_interval}")

# # #         # set variable configuration parameters
# # #         if self._set_config:
# # #             for cmd in self._set_config:
# # #                 response = self.send_command(cmd)
# # #                 if "ok" not in response.lower():
# # #                     self.logger.warning(f"Command '{cmd}' returned unexpected response: {response}")

# # #         # persist the configuration
# # #         self.send_command("set save params")

# # #         self._header = "pcdate pctime time date flags o3 hio3 cellai cellbi bncht lmpt o3lt flowa flowb pres"

# # #         self._data = str()
# # #         self._saved_data_path = Path()


# # #     def _serial_comm(self, cmd: str) -> str:
# # #         """Low-level serial command execution."""
# # #         self._serial.open()
# # #         self._serial.write(bytes([self._id]) + (f"{cmd}\x0D").encode())
# # #         time.sleep(0.5)
# # #         rcvd = b""
# # #         while self._serial.in_waiting > 0:
# # #             rcvd += self._serial.read(1024)
# # #         self._serial.close()
# # #         return rcvd.decode().split("*")[0].replace(cmd, "").strip()


# # #     def _socket_comm(self, cmd: str) -> str:
# # #         if self._sockmode == "tcp":
# # #             return self._tcp_comm(cmd)
# # #         else:
# # #             raise NotImplementedError("TCP/IP is the only supported socket protocol for Thermo instruments.")


# # #     def _tcp_comm(self, cmd: str) -> str:
# # #         """Low-level TCP/IP command execution."""
# # #         with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
# # #             s.settimeout(self._socktout)
# # #             s.connect(self._sockaddr)
# # #             s.sendall(bytes([self._id]) + (f"{cmd}\x0D").encode())
# # #             time.sleep(self._socksleep)
# # #             rcvd = b""
# # #             while True:
# # #                 data = s.recv(1024)
# # #                 rcvd += data
# # #                 if b'\x00' in data:
# # #                     break
# # #         return rcvd.decode().split("*")[0].replace(cmd, "").strip()


# # #     def get_data(self) -> str:
# # #         """
# # #         Read measurement data from instrument and add timestamp.

# # #         Returns:
# # #             str: Timestamped response string.
# # #         """
# # #         now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
# # #         result = self.send_command(self._get_data)
# # #         self._data += f"{now} {result}\n"
# # #         return result


# # #     def accumulate_data(self, data: str):
# # #         """
# # #         Append data to internal buffer.

# # #         Args:
# # #             data (str): Line of instrument output.
# # #         """
# # #         self._data += data + "\n"


# # #     def get_all_lrec(self, save: bool=True) -> str:
# # #         """download entire buffer from instrument and save to file

# # #         :param bln save: Should data be saved to file? default=True
# # #         :return str response as decoded string
# # #         """
# # #         try:
# # #             data = str()

# # #             # get current lrec format, then set lrec format
# # #             cmd = "lrec format"
# # #             lrec_format = self.send_command(cmd)
# # #             _ = self.send_command(f"set {cmd} 0")
# # #             if not 'ok' in _:
# # #                 self.logger.warning(f"'set {cmd} 0' returned '{_}' instead of 'ok'.")

# # #             # retrieve numbers of lrec stored in buffer
# # #             cmd = "no of lrec"
# # #             no_of_lrec = int(self.send_command(cmd).split()[0])

# # #             # retrieve all lrec records stored in buffer
# # #             index = no_of_lrec
# # #             batch_size = 10

# # #             while index > 0:
# # #                 if index < 10:
# # #                     batch_size = index
# # #                 cmd = f"lrec {str(index)} {str(batch_size)}"
# # #                 self.logger.info(cmd)
# # #                 data += f"{self.send_command(cmd)}\n"

# # #                 # remove all the extra info in the string returned
# # #                 # 05:26 07-19-22 flags 0C100400 o3 30.781 hio3 0.000 cellai 50927 cellbi 51732 bncht 29.9 lmpt 53.1 o3lt 0.0 flowa 0.435 flowb 0.000 pres 493.7
# # #                 # data = data.replace("flags ", "")
# # #                 # data = data.replace("hio3 ", "")
# # #                 # data = data.replace("cellai ", "")
# # #                 # data = data.replace("cellbi ", "")
# # #                 # data = data.replace("bncht ", "")
# # #                 # data = data.replace("lmpt ", "")
# # #                 # data = data.replace("o3lt ", "")
# # #                 # data = data.replace("flowa ", "")
# # #                 # data = data.replace("flowb ", "")
# # #                 # data = data.replace("pres ", "")
# # #                 # data = data.replace("o3 ", "")

# # #                 index = index - batch_size

# # #             if save:
# # #                 self.save_data_file()
# # #                 self.stage_data_file()

# # #             # restore lrec format
# # #             _ = self.send_command(f'set {lrec_format}')
# # #             if not 'ok' in _:
# # #                 self.logger.warning(f"'set {lrec_format}' returned '{_}' instead of 'ok'.")

# # #             return data

# # #         except Exception as err:
# # #             self.logger.error(err)
# # #             return str()


# # #     def get_o3(self) -> str:
# # #         try:
# # #             if self.simulate:
# # #                 return str(random.randint(0, 1000) / 10)  # Simulate a random response
# # #             else:
# # #                 if self._params_comms:
# # #                     return self._serial_comm('o3')
# # #                 else:
# # #                     return self._socket_comm('o3')

# # #         except Exception as err:
# # #             self.logger.error(err)
# # #             return str()


# # #     def print_o3(self) -> None:
# # #         try:
# # #             if self._params_comms:
# # #                 o3 = self._serial_comm('o3').split()
# # #             else:
# # #                 o3 = self._socket_comm('o3').split()
# # #             self.logger.info(colorama.Fore.GREEN + f"{self._name}, {o3[0].upper()} {str(float(o3[1]))} {o3[2]}")

# # #         except Exception as err:
# # #             self.logger.error(colorama.Fore.RED + f"{err}")


# # #     def set_o3(self, level: str) -> str:
# # #         try:
# # #             if self.simulate:
# # #                 return level
# # #             else:
# # #                 if self._params_comms:
# # #                     return self._serial_comm(f"set o3 {level}")
# # #                 else:
# # #                     return self._socket_comm(f"set o3 {level}")

# # #         except Exception as err:
# # #             self.logger.error(err)
# # #             return str()


# # #     # def stage_data_file(self, data=None, destination=None):
# # #     #     """
# # #     #     Create a zip archive from the last saved file and move it to staging.
# # #     #     """
# # #     #     try:
# # #     #         self.save_data()
# # #     #         if self._saved_data_path is not None:
# # #     #             archive = self.staging_path / self._saved_data_path.name.replace(".dat", ".zip")
# # #     #             with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
# # #     #                 zf.write(str(self._saved_data_path), arcname=self._saved_data_path.name)
# # #     #             self.logger.info(f"Staged file to {archive}")
# # #     #         else:
# # #     #             self.logger.error("No saved data file to stage.")
# # #     #     except Exception as e:
# # #     #         self.logger.error(f"Failed to stage file: {e}")

# # #     def set_datetime(self):
# # #         """
# # #         Set the instrument clock to system date and time.
# # #         """
# # #         try:
# # #             date_cmd = f"set date {time.strftime('%m-%d-%y')}"
# # #             time_cmd = f"set time {time.strftime('%H:%M:%S')}"

# # #             date_result = self.send_command(date_cmd)
# # #             self.logger.info(f"{self._name}, Date set to: {date_result}")
# # #             time_result = self.send_command(time_cmd)
# # #             self.logger.info(f"{self._name}, Time set to: {time_result}")

# # #         except Exception as err:
# # #             self.logger.error(err)

# # #     def get_config(self) -> dict:
# # #         """
# # #         Read configuration settings from the instrument.

# # #         Returns:
# # #             dict: Mapping of command to returned value.
# # #         """
# # #         cfg = {}
# # #         try:
# # #             for cmd in self._get_config:
# # #                 cfg[cmd] = self.send_command(cmd)
# # #             self.logger.info(f"{self._name}, Configuration read as: {cfg}")
# # #             return cfg
# # #         except Exception as err:
# # #             self.logger.error(err)
# # #             return {}

# # #     def set_config(self) -> dict:
# # #         """
# # #         Send configuration settings to the instrument.

# # #         Returns:
# # #             dict: Mapping of command to response.
# # #         """
# # #         self.logger.info(f"{self._name}, Setting configuration")
# # #         cfg = {}
# # #         try:
# # #             for cmd in self._set_config:
# # #                 cfg[cmd] = self.send_command(cmd)
# # #                 time.sleep(1)
# # #             self.logger.info(f"{self._name}, Configuration set: {cfg}")
# # #             return cfg
# # #         except Exception as err:
# # #             self.logger.error(err)
# # #             return {}
