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
    def __init__(self, ip: str, port: int = 502, unit_id: int = 1):
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
            response = self.client.read_holding_registers(address, count, unit=self.unit_id)
            if response.isError():
                raise ModbusException(f"Error reading registers at {address}: {response}")
            return response.registers
        except ModbusException as e:
            print(f"Modbus error: {e}")
            return None

    def write_single_register(self, address: int, value: int):
        """Write a single value to one holding register."""
        try:
            response = self.client.write_register(address, value, unit=self.unit_id)
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


if __name__ == "__main__":
    ip = "192.168.100.3"  # your instrument's IP
    port = 502            # default Modbus TCP port
    unit_id = 1           # check your instrument docs

    with ModbusTCPDriver(ip, port, unit_id) as driver:
        registers = driver.read_holding_registers(address=0, count=10)
        if registers is not None:
            print("Register values:", registers)
        else:
            print("Failed to read registers")
