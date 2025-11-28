"""
instr/fidas.py

Define a class Fidas(Instrument) facilitating data acquisition from a Fidas OPC.

Prerequisites:
Instrument setup
> 'accessories' > IADS > change from 'remove volatile/moisture compensation' to OFF
> Control Panel > ...

@author: joerg.klausen@meteoswiss.ch
"""

import datetime
import socket
import time
from pathlib import Path
from typing import Any

import colorama
import polars as pl
import schedule

from instr.instrument import Instrument
from utils.logging import setup_logging


class Fidas(Instrument):

    # Text file format Fidas:
    header = ['Date',
              'Time',
              'Comment',
              'PM1',
              'PM2.5',
              'PM4',
              'PM10',
              'PMtotal',
              'Number Concentration',
              'Humidity',
              'Temperature',
              'Pressure',
              'Flow',
              'Coincidence',
              'Pumps',
              'Weather station',
              'IADS',
              'Calibration',
              'LED',
              'Operating mode',
              'Device status',
              'PM1',
              'PM2.5',
              'PM4',
              'PM10',
              'PMtotal',
              'PM1_classic',
              'PM2.5_classic',
              'PM4_classic',
              'PM10_classic',
              'PMtotal_classic',
              'PMthoraic',
              'PMalveo',
              'PMrespirable',
              'Flowrate',
              'Velocity',
              'Coincidence',
              'Pump_output',
              'IADS_temperature',
              'Raw channel deviation',
              'LED temperature',
              'Temperature*',
              'Humidity*',
              'Pressure*',]

    device_status = {'Scope':0,
                    'Auto':1,
                    'Manual':2,
                    'Idle':3,
                    'Calib':4,
                    'Offset':5,
                    'PDControl':6,
                    }


    def __init__(self, name: str, config_path: str):
        super().__init__(name, config_path)
        colorama.init(autoreset=True)

        self.name = name

        # configure logging
        # _logger = config['logging']['file'].split('.')[0]
        # self.logger = setup_logging(file=str(Path(config['root']).expanduser() / f"{name}.log"))
        # # self.logger = logging.getLogger(f"{_logger}.{__name__}")
        # self.logger.info(f"[{self.name}] Initializing")

        # self.host = config[name]['socket']['host']
        # self.port = config[name]['socket']['port']
        # self.buffer_size = config[name]['socket']['buffer_size']

        # self.raw_record_interval = self._params['raw_record_interval']

        self.sock = None
        self.buffer = ""
        self.parsed_record: dict[str, Any] = {}
        self.parsed_records: list[dict[str, Any]] = []
        self.df_raw_data_median = pl.DataFrame()
        self.current_hour = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)

    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(self._sockaddr)
        self.logger.info(f"Listening on {self._sockaddr}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.sock:
            self.sock.close()

    @staticmethod
    def _parse_reply(record: str) -> "dict[str, Any]":
        id_part, rest = record.split('<', 1)
        data_part, checksum = rest.split('>', 1)
        parsed_record = {"id": int(id_part.strip()), "checksum": checksum.strip()}

        if data_part.startswith("sendVal"):
            data_part = data_part[len("sendVal"):].strip()

        for pair in data_part.split(';'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                key = f"{int(k.strip())}"
                try:
                    val = float(v.strip())
                except ValueError:
                    val = float('nan')
                parsed_record[key] = val
        return parsed_record

    # not used
    def _serial_comm(self, cmd: str) -> str:
        self.logger.warning(f"[{self.name}] _serial_comm() is not implemented. Returning empty string.")
        return str()
    
    def _socket_comm(self, cmd: str=str()) -> str:
        """Retrieve a raw record from the instrument, typically passed on to _parse_reply().

        Args:
            cmd (str, optional): Instrument is in streaming mode, all commands are ignored. Defaults to str().

        Returns:
            str: Untreated instrument record
        """

        if self.sock is None:
            return str()
        try:
            self.sock.settimeout(self.sampling_interval_seconds)
            while True:
                data, _ = self.sock.recvfrom(self._params["buffer_size"])
                self.buffer += data.decode('ascii', errors='ignore')
                if '>' in self.buffer:
                    raw_record = self.buffer
                    self.buffer = str()
                    return raw_record
        except socket.timeout:
            pass
        return str()

    def accumulate_data(self, data: str) -> None:
        self.logger.warning(f"[{self.name}] accumulate_data() is not implemented. Returning None.")
        return None
    
    def get_config(self) -> dict:
        self.logger.warning(f"[{self.name}] get_config() is not implemented. Returning empty dict().")
        return dict()

    def set_config(self) -> dict:
        self.logger.warning(f"[{self.name}] set_config() is not implemented. Returning empty dict().")
        return dict()

    def set_datetime(self) -> dict:
        self.logger.warning(f"[{self.name}] set_datetime() is not implemented. Returning empty dict().")
        return dict()

    def get_data(self) -> dict[str, Any]:
        self.raw_record = self._socket_comm()
        
        self.logger.debug(self.raw_record[:90])
        
        if self.raw_record:
            parsed_record = self._parse_reply(self.raw_record)
            if parsed_record:
                self.parsed_records.append(parsed_record)
                # self.logger.debug("[get_data] raw_record appended")
            return parsed_record
        else:
            self.logger.warning("[get_data] raw_record is empty")
            return dict()

    def print_parsed_record(self, keys=['60', '61', '62', '63', '64']):
        """Print latest parsed record"""
        if self.parsed_record:
            result = "; ".join(f"{k}: {int(self.parsed_record[k])}" for k in keys if k in self.parsed_record and self.parsed_record[k] is not None)
            self.logger.debug(colorama.Fore.GREEN + f"[{self.name}] {result}")
        else:
            self.logger.warning(colorama.Fore.YELLOW + f"[{self.name}] no valid data retrieved." + colorama.Fore.GREEN)

    def compute_raw_data_median(self, cols: list=['60','61','62','63','64','65']) -> dict:
        self.logger.debug(f"[compute_raw_data_median] called")
        if not self.parsed_records:
            self.logger.debug("[compute_raw_data_median] self.parsed_records is empty.")
            return dict()

        df = pl.DataFrame(self.parsed_records)
        value_cols = [col for col in df.columns if col not in {"id", "checksum"} and df.schema[col] in {pl.Float64, pl.Float32}]

        median_row = df.select([pl.median(col).alias(col) for col in value_cols])
        now = datetime.datetime.now(datetime.timezone.utc)

        median_row = median_row.with_columns([
            pl.lit("median").alias("id"),
            pl.lit("").alias("checksum"),
            pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("dtm")
        ])

        for col in df.columns:
            if col not in median_row.columns:
                median_row = median_row.with_columns(pl.lit(None).alias(col))

        median_row = median_row.select(sorted(median_row.columns))
        median_dict = {col: median_row[0, col] for col in cols}
        self.df_raw_data_median = pl.concat([self.df_raw_data_median, median_row], how="diagonal")
        self.parsed_records.clear()

        # self.logger.info(f"[compute_raw_data_median] df_median contains {len(self.df_raw_data_median)} rows.")
        self.logger.info(f"[{self.name}] median  {median_dict}.")

        return median_dict

    def save_hourly(self, stage: bool=True):
        self.logger.debug(f"[save_hourly] called")
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.hour != self.current_hour.hour:
            if not self.df_raw_data_median.is_empty():
                data_path = self.ensure_data_path(self.current_hour)
                if data_path.exists():
                    existing = pl.read_parquet(data_path)
                    self.df_raw_data_median = pl.concat([existing, self.df_raw_data_median], how="diagonal").unique()
                self.df_raw_data_median.write_parquet(data_path)
                self.logger.info(f"Saved hourly file: {data_path}")
                if stage:
                    staging_path = self.ensure_staging_path(self.current_hour)
                    self.df_raw_data_median.write_parquet(staging_path)
                    self.logger.info(f"Staged hourly file: {staging_path}")

            self.df_raw_data_median = pl.DataFrame()
            self.current_hour = now.replace(minute=0, second=0, microsecond=0)

    def ensure_data_path(self, dt: datetime.datetime) -> Path:
        folder = self.data_path / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"fidas-{dt.year:04d}{dt.month:02d}{dt.day:02d}{dt.hour:02d}.parquet"
        return folder / filename

    def ensure_staging_path(self, dt: datetime.datetime) -> Path:
        folder = self.staging_path
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"fidas-{dt.year:04d}{dt.month:02d}{dt.day:02d}{dt.hour:02d}.parquet"
        return folder / filename

    def setup_schedules(self):
        try:
            schedule.every(self.sampling_interval_seconds).seconds.do(self.get_data)
            schedule.every(self.aggregation_period).minutes.do(self.compute_raw_data_median)
            schedule.every(1).hours.do(self.save_hourly, stage=True)
            self.logger.info(schedule.get_jobs())
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

    def run(self):
        self.logger.info("=== Starting FIDAS DAQ =======")
        # print("=== Starting FIDAS DAQ =======")
        schedule.every(self.sampling_interval_seconds).seconds.do(self.get_data)
        schedule.every(self.aggregation_period).minutes.do(self.compute_raw_data_median)
        schedule.every(1).hours.do(self.save_hourly, stage=True)
        self.logger.info(schedule.get_jobs())
        # print(schedule.get_jobs())

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("Stopping FIDAS...")
            self.save_hourly()  # Save any remaining data on exit


if __name__ == "__main__":
    pass


# import datetime as dt
# import logging
# import logging.handlers
# import os
# import shutil
# import socket
# import struct
# import time
# import warnings
# import zipfile

# import colorama
# # from pymodbus.client.tcp import ModbusTcpClient
# # from pymodbus.exceptions import ModbusException

# # Instrument setup
# # > 'accessories' > IADS > change from 'remove volatile/moisture compensation' to OFF
# # > Control Panel >


# # Text file format Fidas:
# header = ['Date',
# 'Time',
# 'Comment',
# 'PM1',
# 'PM2.5',
# 'PM4',
# 'PM10',
# 'PMtotal',
# 'Number Concentration',
# 'Humidity',
# 'Temperature',
# 'Pressure',
# 'Flow',
# 'Coincidence',
# 'Pumps',
# 'Weather station',
# 'IADS',
# 'Calibration',
# 'LED',
# 'Operating mode',
# 'Device status',
# 'PM1',
# 'PM2.5',
# 'PM4',
# 'PM10',
# 'PMtotal',
# 'PM1_classic',
# 'PM2.5_classic',
# 'PM4_classic',
# 'PM10_classic',
# 'PMtotal_classic',
# 'PMthoraic',
# 'PMalveo',
# 'PMrespirable',
# 'Flowrate',
# 'Velocity',
# 'Coincidence',
# 'Pump_output',
# 'IADS_temperature',
# 'Raw channel deviation',
# 'LED temperature',
# 'Temperature*',
# 'Humidity*',
# 'Pressure*',]

# device_status = {'Scope':0,
#                  'Auto':1,
#                  'Manual':2,
#                  'Idle':3,
#                  'Calib':4,
#                  'Offset':5,
#                  'PDControl':6,
#                  }


# # class ModbusTCPDriver:
# #     def __init__(self, ip: str, port: int = 11231, unit_id: int = 1):
# #         """
# #         Initialize a Modbus TCP connection.

# #         Args:
# #             ip (str): IP address of the Modbus instrument.
# #             port (int): TCP port number (default 502).
# #             unit_id (int): Modbus slave/unit ID.
# #         """
# #         self.ip = ip
# #         self.port = port
# #         self.unit_id = unit_id
# #         self.client = ModbusTcpClient(ip, port=port)
# #         self.connected = False

# #     def connect(self):
# #         """Establish the TCP connection."""
# #         self.connected = self.client.connect()
# #         if not self.connected:
# #             raise ConnectionError(f"Failed to connect to {self.ip}:{self.port}")

# #     def close(self):
# #         """Close the TCP connection."""
# #         self.client.close()
# #         self.connected = False

# #     def read_holding_registers(self, address: int, count: int):
# #         """Read holding registers starting at address."""
# #         try:
# #             response = self.client.read_holding_registers(address=address, count=count, slave=self.unit_id)
# #             if response.isError():
# #                 raise ModbusException(f"Error reading registers at {address}: {response}")
# #             return response.registers
# #         except ModbusException as e:
# #             print(f"Modbus error: {e}")
# #             return None

# #     def write_single_register(self, address: int, value: int):
# #         """Write a single value to one holding register."""
# #         try:
# #             response = self.client.write_register(address=address, value=value, slave=self.unit_id)
# #             if response.isError():
# #                 raise ModbusException(f"Error writing to register {address}: {response}")
# #             return True
# #         except ModbusException as e:
# #             print(f"Modbus error: {e}")
# #             return False

# #     def __enter__(self):
# #         self.connect()
# #         return self

# #     def __exit__(self, exc_type, exc_val, exc_tb):
# #         self.close()






# import argparse
# import logging
# import os
# import re
# import time
# from datetime import datetime
# from typing import Callable

# import polars as pl
# import schedule


# def setup_logging() -> None:
#     logging.basicConfig(
#         level=logging.INFO,
#         format="%(asctime)s [%(levelname)s] %(message)s",
#     )


# def read_from_instrument() -> str:
#     # Replace this with your actual instrument I/O
#         """
#         Establish a connection and retrieve a record.

#         :return: response of instrument, decoded
#         """
#         rcvd = str()
#         _socktout = 2
#         _sockaddr = ('192.168.2.129', 56790)

#         try:
#             # open socket connection as a client
#             with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, ) as s:
#                 # connect to the server
#                 s.settimeout(_socktout)
#                 s.bind(_sockaddr)

#                 while True:
#                     data, addr = s.recvfrom(1024)
#                     if '>' in data.decode():
#                         rcvd = f"{rcvd}{data.decode()}"
#                         break

#             print(f"{time.time()} {rcvd}")

#             return rcvd

#         except Exception as err:
#             print(err)
#             return str()
#     # return '6082<sendVal 0=0.0;1=1.0;2=2.0;8=4.8;14=42.4;74=0.0>3E'


# def collect_and_aggregate_polars(
#     read_func: Callable[[], str],
#     sampling_interval_seconds: int,
#     output_dir: str
# ) -> None:
#     """
#     Collects instrument data for 1 minute, parses into a Polars DataFrame,
#     computes medians, and saves results to a timestamped CSV file.
#     """
#     logging.info("Collecting data...")
#     rows = []
#     end_time = time.time() + 60

#     while time.time() < end_time:
#         line = read_func()
#         match = re.search(r"<sendVal (.+?)>", line)
#         if match:
#             payload = match.group(1)
#             parsed = {}
#             for item in payload.split(";"):
#                 if "=" not in item:
#                     continue
#                 key_str, value_str = item.split("=")
#                 try:
#                     key = f"v{int(key_str)}"
#                     value = float(value_str)
#                     if not value_str.lower() == "nan":
#                         parsed[key] = value
#                 except ValueError:
#                     continue
#             if parsed:
#                 rows.append(parsed)
#         time.sleep(sampling_interval_seconds)

#     if not rows:
#         logging.warning("No valid data collected in this interval.")
#         return

#     df = pl.DataFrame(rows).fill_nan(None)
#     median_row = df.select(pl.all().median()).to_dict(as_series=False)

#     now = dt.datetime.now(dt.timezone.utc)
#     timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
#     filename = os.path.join(output_dir, f"fidas-{now.strftime('%Y%m%d%H')}.csv")

#     sorted_keys = sorted(median_row.keys())
#     file_exists = os.path.exists(filename)

#     os.makedirs(output_dir, exist_ok=True)
#     with open(filename, "a") as f:
#         if not file_exists:
#             f.write("timestamp," + ",".join(sorted_keys) + "\n")
#         line = timestamp + "," + ",".join(
#             f"{median_row[k]:.4f}" if median_row[k] is not None else "NaN"
#             for k in sorted_keys
#         )
#         f.write(line + "\n")

#     logging.info("Wrote 1-minute aggregate to %s", filename)


# def main():
#     parser = argparse.ArgumentParser(description="Fidas Data Collector")
#     parser.add_argument("--interval", type=int, default=5,
#                         help="Raw data sampling interval in seconds (default: 5)")
#     parser.add_argument("--output", type=str, default=".",
#                         help="Output directory for CSV files")
#     args = parser.parse_args()

#     setup_logging()
#     logging.info("Starting Fidas data collector...")
#     schedule.every(1).minutes.do(
#         collect_and_aggregate_polars,
#         read_func=read_from_instrument,
#         sampling_interval_seconds=args.interval,
#         output_dir=args.output
#     )

#     while True:
#         schedule.run_pending()
#         time.sleep(1)


# if __name__ == "__main__":
#     main()


# # if __name__ == "__main__":
#     # ip = "192.168.0.216"  # your instrument's IP
#     # port = 502            # default Modbus TCP port
#     # unit_id = 1           # check your instrument docs

#     # with ModbusTCPDriver(ip, port, unit_id) as driver:
#     #     registers = driver.read_holding_registers(address=0, count=10)
#     #     if registers is not None:
#     #         print("Register values:", registers)
#     #     else:
#     #         print("Failed to read registers")



# from pymodbus.client.tcp import ModbusTcpClient
# from pymodbus.exceptions import ModbusException

# class ModbusTCPDriver:
#     def __init__(self, ip: str, port: int = 11231, unit_id: int = 1):
#         """
#         Initialize a Modbus TCP connection.

#         Args:
#             ip (str): IP address of the Modbus instrument.
#             port (int): TCP port number (default 502).
#             unit_id (int): Modbus slave/unit ID.
#         """
#         self.ip = ip
#         self.port = port
#         self.unit_id = unit_id
#         self.client = ModbusTcpClient(ip, port=port)
#         self.connected = False

#     def connect(self):
#         """Establish the TCP connection."""
#         self.connected = self.client.connect()
#         if not self.connected:
#             raise ConnectionError(f"Failed to connect to {self.ip}:{self.port}")

#     def close(self):
#         """Close the TCP connection."""
#         self.client.close()
#         self.connected = False

#     def read_holding_registers(self, address: int, count: int):
#         """Read holding registers starting at address."""
#         try:
#             response = self.client.read_holding_registers(address=address, count=count, slave=self.unit_id)
#             if response.isError():
#                 raise ModbusException(f"Error reading registers at {address}: {response}")
#             return response.registers
#         except ModbusException as e:
#             print(f"Modbus error: {e}")
#             return None

#     def write_single_register(self, address: int, value: int):
#         """Write a single value to one holding register."""
#         try:
#             response = self.client.write_register(address=address, value=value, slave=self.unit_id)
#             if response.isError():
#                 raise ModbusException(f"Error writing to register {address}: {response}")
#             return True
#         except ModbusException as e:
#             print(f"Modbus error: {e}")
#             return False

#     def __enter__(self):
#         self.connect()
#         return self

#     def __exit__(self, exc_type, exc_val, exc_tb):
#         self.close()

# import os
# import re
# import time
# import logging
# import argparse
# import schedule
# from datetime import datetime
# from typing import Callable
# import polars as pl


# def setup_logging() -> None:
#     logging.basicConfig(
#         level=logging.INFO,
#         format="%(asctime)s [%(levelname)s] %(message)s",
#     )


# def read_from_instrument() -> str:
#     # Replace this with your actual instrument I/O
#     return '6082<sendVal 0=0.0;1=1.0;2=2.0;8=4.8;14=42.4;74=0.0>3E'


# def collect_and_aggregate_polars(
#     read_func: Callable[[], str],
#     interval_seconds: int,
#     output_dir: str
# ) -> None:
#     """
#     Collects instrument data for 1 minute, parses into a Polars DataFrame,
#     computes medians, and saves results to a timestamped CSV file.
#     """
#     logging.info("Collecting data...")
#     rows = []
#     end_time = time.time() + 60

#     while time.time() < end_time:
#         line = read_func()
#         match = re.search(r"<sendVal (.+?)>", line)
#         if match:
#             payload = match.group(1)
#             parsed = {}
#             for item in payload.split(";"):
#                 if "=" not in item:
#                     continue
#                 key_str, value_str = item.split("=")
#                 try:
#                     key = f"v{int(key_str)}"
#                     value = float(value_str)
#                     if not value_str.lower() == "nan":
#                         parsed[key] = value
#                 except ValueError:
#                     continue
#             if parsed:
#                 rows.append(parsed)
#         time.sleep(interval_seconds)

#     if not rows:
#         logging.warning("No valid data collected in this interval.")
#         return

#     df = pl.DataFrame(rows).fill_nan(None)
#     median_row = df.select(pl.all().median()).to_dict(as_series=False)

#     now = datetime.utcnow()
#     timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
#     filename = os.path.join(output_dir, f"fidas-{now.strftime('%Y%m%d%H')}.csv")

#     sorted_keys = sorted(median_row.keys())
#     file_exists = os.path.exists(filename)

#     os.makedirs(output_dir, exist_ok=True)
#     with open(filename, "a") as f:
#         if not file_exists:
#             f.write("timestamp," + ",".join(sorted_keys) + "\n")
#         line = timestamp + "," + ",".join(
#             f"{median_row[k]:.4f}" if median_row[k] is not None else "NaN"
#             for k in sorted_keys
#         )
#         f.write(line + "\n")

#     logging.info("Wrote 1-minute aggregate to %s", filename)


# def main():
#     parser = argparse.ArgumentParser(description="Fidas Data Collector")
#     parser.add_argument("--interval", type=int, default=5,
#                         help="Sampling interval in seconds (default: 5)")
#     parser.add_argument("--output", type=str, default=".",
#                         help="Output directory for CSV files")
#     args = parser.parse_args()

#     setup_logging()
#     logging.info("Starting Fidas data collector...")
#     schedule.every(1).minutes.do(
#         collect_and_aggregate_polars,
#         read_func=read_from_instrument,
#         interval_seconds=args.interval,
#         output_dir=args.output
#     )

#     while True:
#         schedule.run_pending()
#         time.sleep(1)


# if __name__ == "__main__":
#     main()

# if __name__ == "__main__":
#     ip = "192.168.0.216"  # your instrument's IP
#     port = 502            # default Modbus TCP port
#     unit_id = 1           # check your instrument docs

#     with ModbusTCPDriver(ip, port, unit_id) as driver:
#         registers = driver.read_holding_registers(address=0, count=10)
#         if registers is not None:
#             print("Register values:", registers)
#         else:
#             print("Failed to read registers")
