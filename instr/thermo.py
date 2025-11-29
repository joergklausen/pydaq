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
    from serial import SerialTimeoutException # type: ignore
except Exception:  # pragma: no cover
    serial = None

from instr.instrument import Instrument, with_serial


class Thermo(Instrument):
    """Unified Thermo ozone analyzer driver (Thermo 49C / 49i)."""

    def __init__(self, name: str, config_path: str):
        super().__init__(name, config_path)

        self.name = name
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
    # def _serial_comm(self, cmd: str) -> str:
    #     assert self._serial is not None
    #     payload = bytes([int(self._id) & 0xFF]) + (f"{cmd}\r").encode()
    #     try:
    #         if not self._serial.is_open:
    #             self._serial.open()
    #         try:
    #             self._serial.reset_input_buffer()
    #             self._serial.reset_output_buffer()
    #         except Exception:
    #             pass
    #         self._serial.write(payload)
    #         time.sleep(0.5)

    #         rcvd = b""
    #         # last_len = -1
    #         # while True:
    #         #     chunk = self._serial.read(1024)
    #         #     if chunk:
    #         #         rcvd += chunk
    #         #     if len(rcvd) == last_len:
    #         #         break
    #         #     last_len = len(rcvd)
    #         # return self._parse_reply(cmd, bytes(rcvd))
    #         deadline = time.monotonic() + max(self._serial.timeout or 1.0, 1.0)
    #         while time.monotonic() < deadline:
    #             if self._serial.in_waiting:
    #                 rcvd += self._serial.read(self._serial.in_waiting)
    #                 if b"*" in rcvd or rcvd.endswith(b"\r"):
    #                     break
    #             time.sleep(0.05)

    #         text = rcvd.decode(errors="ignore").split("*")[0].replace(cmd, "").strip()
    #         if text:
    #             return text
    #         exc_cls = getattr(serial, "SerialTimeoutException", TimeoutError)
    #         raise exc_cls("empty response")
    #     except Exception as err:
    #         self.logger.error("serial_comm(%s) failed: %s", cmd, err)
    #         try:
    #             self._serial.close()
    #         except Exception:
    #             pass
    #         return ""
    # def serial_comm(self, cmd: str) -> str:
    #     _id = bytes([self._id])

    #     # clear stale buffers once per call
    #     self._serial.reset_input_buffer()
    #     self._serial.reset_output_buffer()

    #     self._serial.write(_id + (f"{cmd}\r").encode())

    #     rcvd = b""
    #     timeout = self._serial.timeout or 1.5
    #     deadline = time.monotonic() + max(timeout, 1.5)
    #     while time.monotonic() < deadline:
    #         waiting = self._serial.in_waiting
    #         if waiting:
    #             rcvd += self._serial.read(waiting)
    #             if b"*" in rcvd or rcvd.endswith(b"\r"):
    #                 break
    #         time.sleep(0.05)

    #     text = (
    #         rcvd.decode(errors="ignore")
    #         .split("*")[0]
    #         .replace(cmd, "")
    #         .strip()
    #     )
    #     if not text:
    #         raise SerialTimeoutException("empty response")
    #     return text
    @with_serial
    def _serial_comm(self, cmd: str) -> str:
        """Low-level serial command/response using the Thermo protocol.

        The with_serial decorator adds retries, backoff, and cooldown handling.
        """
        if serial is None:
            raise RuntimeError("pyserial is not available; cannot use serial communication.")

        # Type checkers: decorator ensures _serial is a live Serial instance here
        assert self._serial is not None

        # Thermo protocol: leading one-byte ID then ASCII command + CR
        payload = bytes([int(self._id) & 0xFF]) + (f"{cmd}\r").encode()

        # Clear stale buffers once per call (best effort)
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:
            pass

        # Send command
        self._serial.write(payload)

        # Read response until terminator (* or CR) or timeout
        rcvd = b""
        timeout = getattr(self._serial, "timeout", None) or 1.5
        deadline = time.monotonic() + max(timeout, 1.5)
        while time.monotonic() < deadline:
            waiting = self._serial.in_waiting
            if waiting:
                rcvd += self._serial.read(waiting)
                if b"*" in rcvd or rcvd.endswith(b"\r"):
                    break
            time.sleep(0.05)

        text = (
            rcvd.decode(errors="ignore")
            .split("*")[0]
            .replace(cmd, "")
            .strip()
        )
        if not text:
            # Signal a timeout to the decorator so it can retry/cooldown
            raise SerialTimeoutException("empty response")
        return text


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
                if cmd.lower() in {"o3", "lr00", "lrec"}:
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
            self.logger.error(f"[{self.name}] {err}")

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
            self.logger.error(f"[{self.name}] {err}")
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
            self.logger.error(f"[{self.name}] {err}")
            return {}


    def get_o3(self) -> str:
        return self.send_command("o3")

    def set_o3(self, level: str) -> str:
        return self.send_command(f"set o3 {level}")

    # def print_o3(self) -> str:
    #     val = self.get_o3()
    #     return f"O3: {val}" if val else "O3: <no data>"
    def display_data(self) -> None:
        acquired = self._io_lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            o3 = self.get_o3().split()
            if len(o3) == 2:
                self.logger.info(f"[{self.name}] O3 {float(o3[0]):0.1f} {o3[1]}")
            elif len(o3) == 3:
                self.logger.info(f"[{self.name}] {o3[0].upper()} {float(o3[1]):0.1f} {o3[2]}")
        except Exception as err:
            self.logger.error(f"[{self.name}] print_o3: {err}")
        finally:
            if acquired:
                self._io_lock.release()
