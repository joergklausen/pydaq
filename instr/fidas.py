# Instrument setup
# > 'accessories' > IADS > change from 'remove volatile/moisture compensation' to OFF
# > Control Panel >


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


from pymodbus.client.tcp import ModbusTcpClient
from pymodbus.exceptions import ModbusException

class ModbusTCPDriver:
    def __init__(self, ip: str, port: int = 11231, unit_id: int = 1):
        """
        Initialize a Modbus TCP connection.

        Args:
            ip (str): IP address of the Modbus instrument.
            port (int): TCP port number (default 502).
            unit_id (int): Modbus slave/unit ID.
        """
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self.client = ModbusTcpClient(ip, port=port)
        self.connected = False

    def connect(self):
        """Establish the TCP connection."""
        self.connected = self.client.connect()
        if not self.connected:
            raise ConnectionError(f"Failed to connect to {self.ip}:{self.port}")

    def close(self):
        """Close the TCP connection."""
        self.client.close()
        self.connected = False

    def read_holding_registers(self, address: int, count: int):
        """Read holding registers starting at address."""
        try:
            response = self.client.read_holding_registers(address=address, count=count, slave=self.unit_id)
            if response.isError():
                raise ModbusException(f"Error reading registers at {address}: {response}")
            return response.registers
        except ModbusException as e:
            print(f"Modbus error: {e}")
            return None

    def write_single_register(self, address: int, value: int):
        """Write a single value to one holding register."""
        try:
            response = self.client.write_register(address=address, value=value, slave=self.unit_id)
            if response.isError():
                raise ModbusException(f"Error writing to register {address}: {response}")
            return True
        except ModbusException as e:
            print(f"Modbus error: {e}")
            return False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()






import os
import re
import time
import logging
import argparse
import schedule
from datetime import datetime
from typing import Callable
import polars as pl


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def read_from_instrument() -> str:
    # Replace this with your actual instrument I/O
    return '6082<sendVal 0=0.0;1=1.0;2=2.0;8=4.8;14=42.4;74=0.0>3E'


def collect_and_aggregate_polars(
    read_func: Callable[[], str],
    interval_seconds: int,
    output_dir: str
) -> None:
    """
    Collects instrument data for 1 minute, parses into a Polars DataFrame,
    computes medians, and saves results to a timestamped CSV file.
    """
    logging.info("Collecting data...")
    rows = []
    end_time = time.time() + 60

    while time.time() < end_time:
        line = read_func()
        match = re.search(r"<sendVal (.+?)>", line)
        if match:
            payload = match.group(1)
            parsed = {}
            for item in payload.split(";"):
                if "=" not in item:
                    continue
                key_str, value_str = item.split("=")
                try:
                    key = f"v{int(key_str)}"
                    value = float(value_str)
                    if not value_str.lower() == "nan":
                        parsed[key] = value
                except ValueError:
                    continue
            if parsed:
                rows.append(parsed)
        time.sleep(interval_seconds)

    if not rows:
        logging.warning("No valid data collected in this interval.")
        return

    df = pl.DataFrame(rows).fill_nan(None)
    median_row = df.select(pl.all().median()).to_dict(as_series=False)

    now = datetime.utcnow()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    filename = os.path.join(output_dir, f"fidas-{now.strftime('%Y%m%d%H')}.csv")

    sorted_keys = sorted(median_row.keys())
    file_exists = os.path.exists(filename)

    os.makedirs(output_dir, exist_ok=True)
    with open(filename, "a") as f:
        if not file_exists:
            f.write("timestamp," + ",".join(sorted_keys) + "\n")
        line = timestamp + "," + ",".join(
            f"{median_row[k]:.4f}" if median_row[k] is not None else "NaN"
            for k in sorted_keys
        )
        f.write(line + "\n")

    logging.info("Wrote 1-minute aggregate to %s", filename)


def main():
    parser = argparse.ArgumentParser(description="Fidas Data Collector")
    parser.add_argument("--interval", type=int, default=5,
                        help="Sampling interval in seconds (default: 5)")
    parser.add_argument("--output", type=str, default=".",
                        help="Output directory for CSV files")
    args = parser.parse_args()

    setup_logging()
    logging.info("Starting Fidas data collector...")
    schedule.every(1).minutes.do(
        collect_and_aggregate_polars,
        read_func=read_from_instrument,
        interval_seconds=args.interval,
        output_dir=args.output
    )

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
















if __name__ == "__main__":
    ip = "192.168.0.216"  # your instrument's IP
    port = 502            # default Modbus TCP port
    unit_id = 1           # check your instrument docs

    with ModbusTCPDriver(ip, port, unit_id) as driver:
        registers = driver.read_holding_registers(address=0, count=10)
        if registers is not None:
            print("Register values:", registers)
        else:
            print("Failed to read registers")
