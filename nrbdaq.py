import os
import schedule
import time
from instr.ae31 import AE31
import instr.avo as avo
from instr.thermo import Thermo49i
from instr.aurora3000 import Aurora3000
from utils.sftp import SFTPClient
from utils.utils import load_config, setup_logging, seconds_to_next_n_minutes


def main():
    # load configuation
    config = load_config(config_file='nrbdaq.yml')

    # setup logging
    logfile = os.path.join(os.path.expanduser(config['root']), config['logging']['file'])
    logger = setup_logging(file=logfile)
    logger.info("== Start NRBDAQ =============")

    # setup sftp client
    sftp = SFTPClient(config=config)
    logger.debug(f"sftp.remote_path: {sftp.remote_path}")

    # setup AE31 data acquisition and data transfer
    ae31 = AE31(config=config)
    ae31.setup_schedules()
    remote_path = os.path.join(sftp.remote_path, ae31.remote_path)
    sftp.setup_transfer_schedules(local_path=ae31.staging_path,
                                  remote_path=remote_path,
                                  interval=ae31.reporting_interval)

    # setup Nairobi AVO data download, staging and transfer
    data_path = os.path.join(os.path.expanduser(config['root']), config['data'], config['AVO']['data_path'])
    staging_path = os.path.join(os.path.expanduser(config['root']), config['staging'], config['AVO']['staging_path'])
    remote_path = os.path.join(sftp.remote_path, config['AVO']['remote_path'])
    download_interval = config['AVO']['download_interval']
    hours = [f"{download_interval*n:02}:00" for n in range(23) if download_interval*n <= 23]
    for hr in hours:
        schedule.every(1).day.at(hr).do(avo.download_multiple,
                                       urls={'url_nairobi': config['AVO']['urls']['url_nairobi']},
                                       file_path=data_path,
                                       staging=staging_path)
    sftp.setup_transfer_schedules(local_path=staging_path,
                                  remote_path=remote_path,
                                  interval=download_interval)

    # setup Thermo 49i data acquisition and data transfer
    thermo49i = Thermo49i(config=config)
    thermo49i.setup_schedules()
    remote_path = os.path.join(sftp.remote_path, thermo49i.remote_path)
    sftp.setup_transfer_schedules(local_path=thermo49i.staging_path,
                                  remote_path=remote_path,
                                  interval=thermo49i.reporting_interval)

    # setup Aurora3000
    neph = Aurora3000(config=config)
    neph.setup_schedules()
    logger.info(f"get_instrument_id: {neph.get_instrument_id()}")
    remote_path = os.path.join(sftp.remote_path, neph.remote_path)
    sftp.setup_transfer_schedules(local_path=neph.staging_path,
                                  remote_path=remote_path,
                                  interval=neph.reporting_interval)

    # list all jobs
    logger.info(schedule.get_jobs())

    # align start with a multiple-of-minute timestamp
    seconds_left = seconds_to_next_n_minutes(1)
    while seconds_left > 0:
        print(f"Time remaining: {seconds_left:0.1f} s", end="\r")
        dt = 0.2
        time.sleep(dt)
        seconds_left -= dt
    logger.info("Beginning data acquisition and file transfer ...")

    # start jobs
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
