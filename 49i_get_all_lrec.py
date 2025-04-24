import os
from instr.thermo import Thermo49i
from utils.utils import load_config
from utils.logging_config import setup_logging


config = load_config(config_file='config.yaml')
tei49i = Thermo49i(config=config, name='49i')

# setup logging
logfile = os.path.join(os.path.expanduser(config['root']), config['logging']['file'])
logger = setup_logging(file=logfile)

tei49i.get_all_lrec()
