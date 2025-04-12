import argparse
import yaml
import schedule
import time
from utils.instrument_loader import load_instrument
from utils.logging_config import setup_logging
from utils.sftp import SFTPClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    setup_logging()

    simulate = args.simulate or config.get("simulate", False)
    data_dir = config.get("paths", {}).get("data", "data")

    instruments = []
    for inst_cfg in config.get("instruments", []):
        class_path = inst_cfg["class"]
        name = inst_cfg["name"]
        params = inst_cfg.get("params", {})
        params["data_dir"] = data_dir
        instrument = load_instrument(class_path, name, params, simulate=simulate)
        instruments.append((instrument, params.get("poll_interval", 60)))

    for inst, interval in instruments:
        schedule.every(interval).seconds.do(inst.acquire_data)

    # setup sftp client
    sftp = SFTPClient(config=config)

    print("Running pydaq... (CTRL+C to exit)")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
