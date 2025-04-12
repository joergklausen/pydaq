import argparse
from instr.thermo import Thermo49i
from utils.logging_config import setup_logging
from utils.utils import load_config

def main():
    parser = argparse.ArgumentParser(
        description='Send command to Thermo 49i and receive response.')
    parser.add_argument("--config", default="config_test.yaml")
    parser.add_argument('--cmd',
                        default="o3",
                        help='Command to be sent (e.g., o3)')
    args = parser.parse_args()

    config = load_config(config_file=args.config)
    tei49i = Thermo49i(config=config)

    # setup logging
    logger = setup_logging(config=config)

    response = tei49i.send_command(cmd=args.cmd)
    logger.info(f"sent: {args.cmd}; rcvd: {response}")


if __name__ == "__main__":
    main()
