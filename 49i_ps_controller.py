import argparse
import csv
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import polars as pl
import yaml
from flask import Flask, jsonify, render_template_string
from scipy.stats import linregress

from instr.thermo import Thermo49i
from utils.logging_config import setup_logging
from utils.utils import load_config

# ---------------- Placeholder for your driver functions ------------------
# def set_value(instrument_ip, value_ppb):
#     print(f"[DEBUG] Set {value_ppb} ppb on {instrument_ip}")
#     # Replace with actual command to send to instrument

# def get_value(instrument_ip):
#     # Replace with actual read command; here we mock with a float
#     return round(100 + hash(instrument_ip + str(time.time())) % 100 / 10.0, 2)

# ---------------- Load instrument config from YAML ------------------
# def load_config(yaml_file):
#     with open(yaml_file, 'r') as f:
#         return yaml.safe_load(f)

# ---------------- Data Logger and Plotter ------------------
class InstrumentController:
    def __init__(self, config_path, calibrator, values, duration,
                 analyzer=None, output_csv=None):

        self.level = int()

        self.config = load_config(config_path)

        # setup logging
        logfile = os.path.join(os.path.expanduser(self.config['paths']['root']), self.config['logging']['file_name'])
        self.logger = setup_logging(self.config)

        # Get calibrator name, IP, levels, duration
        self.calibrator = Thermo49i(config=self.config, name=(calibrator or "49i_ps"))
        self.calibrator_name = self.calibrator.name
        # self.calibrator_ip = self.calibrator._sockaddr
        self.values = values  # list of setpoints in ppb
        self.duration = duration

        # Get analyzer name, IP
        self.analyzer = Thermo49i(config=self.config, name=(analyzer or "49i"))
        self.analyzer_name = self.analyzer.name
        # self.analyzer_ip = self.analyzer._sockaddr

        self.output_csv =  output_csv or os.path.join(os.path.expanduser(self.config['paths']['root']), self.config['paths']['data'], f"ozone_comparison-{datetime.now().strftime("%Y%m%d%H%M")}.csv")
        os.makedirs(os.path.dirname(self.output_csv), exist_ok=True)

        self.running = False
        self.data = []  # list of dicts with dtm and readings

        # CSV writer thread-safe setup
        self.csv_lock = threading.Lock()

        # Flask app setup
        self.app = Flask(__name__)
        self._setup_routes()
        self.flask_thread = threading.Thread(target=self._run_flask)
        self.flask_thread.daemon = True

    def _setup_routes(self):
        @self.app.route('/')
        def dashboard():
            return render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Live Dashboard</title>
                <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
            </head>
            <body>
                <h2>Live Instrument Data</h2>
                <div id="plot" style="width:100%;height:80vh;"></div>
                <script>
                    function fetchData() {
                        fetch('/data').then(res => res.json()).then(data => {
                            const layout = {title: 'Live Instrument Values', xaxis: {title: 'Time'}, yaxis: {title: 'ppb'}};
                            const plotData = [
                                {x: data.timestamps, y: data.level, name: 'level', type: 'scatter'},
                                {x: data.timestamps, y: data.calibrator_values, name: data.calibrator_name, type: 'scatter'},
                                {x: data.timestamps, y: data.analyzer_values, name: data.analyzer_name, type: 'scatter'}
                            ];
                           <!-- Plotly.newPlot('plot', plotData, layout); -->
                        });
                    }
                    setInterval(fetchData, 5000);
                    fetchData();
                </script>
            </body>
            </html>
            """)

        @self.app.route('/data')
        def data():
            timestamps = [row['dtm'] for row in self.data[-100:]]
            calibrator_values = [row[self.calibrator_name] for row in self.data[-100:]]
            analyzer_values = [row[self.analyzer_name] for row in self.data[-100:]]
            return jsonify({
                'timestamps': timestamps,
                'calibrator_values': calibrator_values,
                'analyzer_values': analyzer_values,
                'calibrator_name': self.calibrator_name,
                'analyzer_name': self.analyzer_name
            })

    def _run_flask(self):
        self.app.run(port=5000)

    def stop(self, signum=None, frame=None):
        print("\nGracefully stopping...")
        self.running = False

    def log_data(self):
        with open(self.output_csv, 'w', newline='') as csvfile:
            fieldnames = ['dtm', 'level', self.calibrator_name, self.analyzer_name]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            while self.running:
                dtm = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
                calibrator_value = self.calibrator.get_o3()
                analyzer_value = self.analyzer.get_o3()
                row = {'dtm': dtm, 'level': self.level, self.analyzer_name: analyzer_value, self.calibrator_name: calibrator_value}
                with self.csv_lock:
                    writer.writerow(row)
                self.data.append(row)
                time.sleep(5)  # read interval in seconds

            print("Logging stopped. Final data written to CSV.")


    def control_loop(self):
        for value in self.values:
            if not self.running:
                break
            # set_value(self.calibrator_ip, value)
            self.level = self.calibrator.set_o3(value)
            steps = int(self.duration * 60 / 5)
            for _ in range(steps):
                if not self.running:
                    break
                time.sleep(5)

        self.running = False

    def run(self):
        self.running = True
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.flask_thread.start()

        t_log = threading.Thread(target=self.log_data)
        t_ctrl = threading.Thread(target=self.control_loop)
        t_log.start()
        t_ctrl.start()
        t_ctrl.join()
        t_log.join()
        print("Experiment finished.")
        self.plot_data()

    def plot_data(self):
        df = pl.DataFrame(self.data)
        diff_col = f"{self.analyzer_name}-{self.calibrator_name}"
        df = df.with_columns([
            pl.col("dtm").str.to_datetime(),
            (pl.col(self.analyzer_name) - pl.col(self.calibrator_name)).alias(diff_col)
        ])

        df.write_csv(self.output_csv)  # Overwrite with difference column
        diff_plot_df = df.select(["dtm", self.analyzer_name, self.calibrator_name, diff_col]).to_pandas()

    #     # Time series plot
        plt.figure()
        for col in [self.calibrator_name, self.analyzer_name]:
            plt.plot(diff_plot_df['dtm'], diff_plot_df[col], label=col)
        plt.xlabel('Time')
        plt.ylabel('Value (ppb)')
        plt.title('Instrument Readings Over Time')
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.savefig('time_series_plot.png')
        plt.show()

    #     # Difference plot
        plt.figure()
        plt.plot(diff_plot_df['dtm'], diff_plot_df['difference'], label='Difference')
        plt.xlabel('Time')
        plt.ylabel('Difference (ppb)')
        plt.title('Difference Between Instruments')
        plt.grid(True)
        plt.tight_layout()
        plt.savefig('difference_plot.png')
        plt.show()

        # Regression analysis
        slope, intercept, r_value, p_value, std_err = linregress(
            diff_plot_df[self.calibrator_name], diff_plot_df[self.analyzer_name]
        )

        results_text = (
            f"Linear regression between {self.calibrator_name} (x) and {self.analyzer_name} (y):\n"
            f"  Slope: {slope:.4f}\n"
            f"  Intercept: {intercept:.4f}\n"
            f"  R-squared: {r_value**2:.4f}\n"
            f"  P-value: {p_value:.4e}\n"
            f"  Std Error: {std_err:.4f}\n"
        )
        print("\n" + results_text)

        with open("regression_results.txt", "w") as f:
            f.write(results_text)

# ---------------- CLI Entry Point ------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Ozone comparison of analyzer vs calibrator')
    parser.add_argument('--config', default=None, help='Path to YAML config file')
    parser.add_argument('--calibrator', default="49i_ps", help='Name of calibrator according to config file')
    parser.add_argument('--values', default=None, nargs='+', type=int, help='List of setpoints in ppb')
    parser.add_argument('--duration', type=float, default=1, help='Duration for each setpoint in minutes')
    parser.add_argument('--analyzer', default="49i", help='Name of analyzer according to config file')
    parser.add_argument('--output', default=None, help='Output CSV file path')
    args = parser.parse_args()

    config = args.config or 'config_ozone_comparison.yml'
    values = args.values or [500, 100, 200, 30, 50, 70, 10, 40, 20, 60, 90, 80, 0]

    controller = InstrumentController(config_path=config,
                                      calibrator=args.calibrator,values=values, duration=args.duration,
                                      analyzer=args.analyzer,
                                      output_csv=args.output)
    controller.run()
