"""iQAIr Air Visual Outdoor (AVO) data download and parsing. Data are stored in .parquet files."""
import os
import datetime as datetime
import json
import polars as pl
import requests
import shutil

keys = ['instant', 'hourly', 'daily', 'monthly']

def download_data(url: str, validated: bool=False) -> dict:
    """
    Download AVO data from the portal. 
    The most recent 60 instant (1-minute), 48 hourly, 30/31 daily, 12 monthly values available.
    It is unclear how data are validated in detail.

    Args:
        url (str): The API call
        validated (bool, optional): If True, only validated data are retrieved. 
        These cover a shorter period. Defaults to False.

    Returns:
        dict: A nested dictionary
    """
    if validated:
        url = f"{url}/validated_data"

    resp = requests.get(url)
    if resp.ok:
        data = json.loads(resp.text)

    return data


def flatten_data(data: dict, parent_key='', sep='_') -> dict:
    """Flatten a nested JSON object

    Args:
        data (dict): an AVO data set
        parent_key (str, optional): ??. Defaults to ''.
        sep (str, optional): _description_. Defaults to '_'.

    Returns:
        dict: flattened dict
    """
    items = []
    for k, v in data.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_data(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def data_to_dfs(data: dict, file_path: str=str(),
                append: bool=True, remove_duplicates: bool=True, staging: str=str()) -> tuple[str, dict]:
    """
    Saves a flattened dictionary as polars DataFrame. 
    A column dtm (pl.Datetime) is added. Numerical values are all cast to pl.Float32. Otherwise, the original format is preserved.

    Args:
        data (dict): flattened dict of AVO data
        file_path (str, optional): dictionary path for data files. Defaults to str().
        append (bool, optional): Should existing .parquet files be appended? Defaults to True.
        remove_duplicates (bool, optional): Should duplicates be removed? Defaults to True.
        staging (str, optional): Path to staging directory. Defaults to str() (= no staging).

    Returns:
        tuple[str, dict]: station name, dictionary of the various data sets
    """
    if file_path:
        os.makedirs(file_path, exist_ok=True)

    # Extract name
    station = data['name'].lower().replace(' ', '_')

    result = dict()
    
    # Extract and flatten data into a list of several polars DataFrames
    values = [pl.DataFrame([flatten_data(entry) for entry in data['historical'][key]]) for key in keys]
    
    result = dict(zip(keys, values))

    if result:
        for key, value in result.items():
            # Convert ts to pl.Datetime
            value = value.with_columns(pl.col("ts").str.to_datetime().alias('dtm'))

            # cast all numerical columns to Float32 to reduce file size
            value = value.cast({pl.Int64: pl.Float32, pl.Float64: pl.Float32})

            # create file name
            format = "%Y%m" if key=="monthly" else "%Y%m%d"
            dtm = datetime.datetime.now().strftime(format)
            file = os.path.join(file_path, f"{station}_avo_{key}-{dtm}.parquet")
            
            if append:
                if os.path.exists(file):
                    value = pl.concat([pl.read_parquet(file), value], how='diagonal')
            if remove_duplicates:
                value = value.unique()            
            value = value.sort(by=pl.col('dtm'))
            value.write_parquet(file)

            if staging:
                os.makedirs(os.path.join(os.path.expanduser(staging)), exist_ok=True)
                shutil.copy(src=file, dst=os.path.join(os.path.expanduser(staging), os.path.basename(file)))

    return station, result


def download_multiple(urls: dict, file_path: str, staging: str=str()):
    all = list()
    for key, url in urls.items():
        print(f"retrieving from {key}")
        data = download_data(url=url)
        dfs = data_to_dfs(data=data, file_path=file_path, staging=staging)
        if dfs:
            all.append(dfs)
    return all


def compile_data(stations: list[str], source: str, target:str=str(), archive: bool=True) -> dict:
    _ = dict(zip(keys, [pl.DataFrame(schema=dict([('ts', pl.String),
                                                   ('co2', pl.Float32),
                                                   ('pm1', pl.Float32),
                                                   ('pr', pl.Float32),
                                                   ('hm', pl.Float32),
                                                   ('tp', pl.Float32),
                                                   ('pm25_aqius', pl.Float32),
                                                   ('pm25_aqicn', pl.Float32),
                                                   ('pm25_conc', pl.Float32),
                                                   ('pm10_aqius', pl.Float32),
                                                   ('pm10_aqicn', pl.Float32),
                                                   ('pm10_conc', pl.Float32),
                                                   ('dtm', pl.Datetime(time_unit='us', time_zone='UTC'))]),
                                                   )] * 4))

    dfs = dict([(station, _) for station in stations])

    for root, dirs, files in os.walk(source):
        for file in files:
            if any(station in file for station in stations):
                df = pl.read_parquet(os.path.join(root, file))
                df = df.cast({pl.Int64: pl.Float32, pl.Float64: pl.Float32})

                # Some files have ts converted to Datetime already, with or without a dtm column, so we need to make sure these files can be imported.
                if df['ts'].dtype==pl.Datetime:
                    df = df.rename({'ts': 'dtm'})
                elif ('dtm' in df.columns and all(df['dtm'].is_null())) or ('dtm' not in df.columns):
                    df = df.with_columns(pl.col("ts").str.to_datetime().alias('dtm'))
                    df = df.drop('ts')

                # rename columns, drop AQI columns
                df = df.rename({'pm25_conc': 'pm25', 'pm10_conc': 'pm10'})            
                df = df.drop(['pm25_aqius', 'pm25_aqicn', 'pm10_aqius', 'pm10_aqicn'])

                basename_parts = file.split('.')[0].split('_')
                
                # Handle the case where an underscore is present as the last character before the extension
                if '' in basename_parts:
                    basename_parts.remove('') 
                
                n = len(basename_parts)
                station = "_".join(basename_parts[:(n-2)])
                data_type = basename_parts[n-1].split('-')[0]
                
                # append and sort data by dtm, remove duplicates
                try:
                    dfs[station][data_type] = pl.concat([dfs[station][data_type], df], how = 'diagonal').sort(by='dtm').unique()
                    # print(f"Appended data from '{file}'.")
                except Exception as err:
                    print(f"Failed to append data from '{file}'. Error: {err}")
                    pass

        if target:
            for station, data in dfs.items():
                for key, df in data.items():
                    data[key].write_parquet(os.path.join(target, f"{station}_{key}_avo_compiled.parquet"))

    return dfs
