import os
import polars as pl
import unittest
from pydaq.utils.utils import load_config
from pydaq.utils.sftp import SFTPClient
import pydaq.instr.avo as avo
from pydaq.instr.ae31 import AE31
from pydaq.instr.thermo import Thermo49i

config = load_config('nrbdaq.yml')

class TestSFTP(unittest.TestCase):
    def test_config_host(self):
        self.assertEqual(config['sftp']['host'], 'sftp.meteoswiss.ch')

    def test_is_alive(self):
        sftp = SFTPClient(config=config)

        self.assertEqual(sftp.is_alive(), True)

    def test_transfer_single_file(self):
        sftp = SFTPClient(config=config)

        # setup
        file_path = 'nrbdaq/tests/hello_world.txt'
        file_content = 'Hello, world!'
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as fh:
            fh.write(file_content)
            fh.close()

        remotepath = sftp.remote_path
        remote_path = os.path.join(remotepath, os.path.basename(file_path))
        if sftp.remote_item_exists(remote_path=remote_path):
            sftp.remove_remote_item(remote_path=remote_path)

        attr = sftp.put_file(local_path=file_path, remote_path=remotepath)

        self.assertEqual(sftp.remote_item_exists(remote_path=remote_path), True)

        # clean up
        sftp.remove_remote_item(remote_path=remote_path)
        os.remove(path=file_path)


class TestAVO(unittest.TestCase):
    def test_download_data(self):
        data = avo.download_data(url=config['AVO']['urls']['url_nairobi'])
        self.assertEqual(list(data.keys()), ['historical', 'name', 'current'])

    def test_data_to_dfs(self):
        data = avo.download_data(url=config['AVO']['urls']['url_nairobi'])
        station, dfs = avo.data_to_dfs(data=data,
                              file_path=os.path.join(os.path.expanduser(config['root']), config['AVO']['data']),
                              staging=os.path.join(os.path.expanduser(config['root']), config['AVO']['staging']))
        self.assertEqual(station, 'kmd_hq_nairobi')

class TestAE31(unittest.TestCase):
    def test_validate_ae31_csv_file(self):
        ae31 = AE31(config=config)
        valid_file = 'nrbdaq/tests/data/ae31/AE31_20240825.csv'
        df_valid = ae31.csv_to_df(file=valid_file)

        test_file = 'nrbdaq/tests/data/ae31/AE31_20240805.csv'
        df_test = ae31.csv_to_df(file=test_file)

        self.assertEqual(df_valid.schema, df_test.schema)

class TestThermo49i(unittest.TestCase):
    def test_init(self):
        thermo49i = Thermo49i(config=config)

        self.assertEqual(thermo49i._data, str())

if __name__ == "__main__":
    unittest.main(verbosity=2)
