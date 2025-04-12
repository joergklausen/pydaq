import serial
import time
import argparse

def loopback_test(port: str, baudrate: int, timeout: float):
    """
    Perform a loopback test on the specified serial port.

    :param port: The serial port to use (e.g., /dev/ttyUSB0).
    :param baudrate: The baud rate for the serial communication.
    :param timeout: The timeout value for the serial communication.
    """
    try:
        # Open the serial port
        ser = serial.Serial(port, baudrate, timeout=timeout)
        print(f"Opened {port} successfully.")

        # Send a test message
        test_message = "Hello, Serial Port!"
        ser.write(test_message.encode('ascii'))
        print(f"Sent: {test_message}")

        # Give the device some time to respond
        time.sleep(1)

        # Read the response
        if ser.in_waiting > 0:
            response = ser.read(ser.in_waiting).decode('ascii')
            print(f"Received: {response}")

            # Check if the response matches the test message
            if response == test_message:
                print("Loopback test successful!")
            else:
                print("Loopback test failed: received incorrect message.")
        else:
            print("No response received.")

        # Close the serial port
        ser.close()

    except serial.SerialException as e:
        print(f"Serial communication error: {e}")
    except Exception as e:
        print(f"General error: {e}")

def main():
    """
    Main function to parse CLI arguments and perform the loopback test.
    """
    parser = argparse.ArgumentParser(description="Serial port loopback test.")
    parser.add_argument("--port", type=str, default="COM1", required=False, help="Serial port (e.g., /dev/ttyUSB0)")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate (default: 9600)")
    parser.add_argument("--timeout", type=float, default=1.0, help="Timeout in seconds (default: 1.0)")

    args = parser.parse_args()

    loopback_test(args.port, args.baudrate, args.timeout)

if __name__ == "__main__":
    main()
