"""Read-only real Demo parity check; no training, accounts or network required."""
import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo_dataset import build_demo_csv
from demo_live_dataset import build_live_csv, PARSER_VERSION
from train_decision_model import load_dataset
from upload_protocol import COLUMNS, validate_row
import trainer_gui


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demos', nargs='+', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for number, demo in enumerate(args.demos, 1):
        folder = args.out / str(number)
        folder.mkdir(exist_ok=True)
        started = time.perf_counter()
        worker = folder / 'worker.csv'
        desktop = folder / 'desktop.csv'
        cli = folder / 'trainer-cli.csv'
        worker_stats = build_live_csv(demo, worker)
        desktop_stats = build_demo_csv([demo], desktop)
        with mock.patch('trainer_gui.train_from_csv') as train:
            code = trainer_gui._demo_cli(['--demos', str(demo), '--demo-csv', str(cli)])
            assert code == 0
            train.assert_called_once()
        assert sha(worker) == sha(desktop) == sha(cli), 'entry-point byte mismatch'
        assert load_dataset(worker) == load_dataset(desktop) == load_dataset(cli)
        with desktop.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            validate_row(row)
        # The existing worker projects to compatibility columns and adds its
        # server match identity; all retained values must be unchanged.
        assert all(set(COLUMNS) <= set(row) for row in rows)
        results.append({
            'source_name': demo.name, 'source_sha256': worker_stats['source_sha256'],
            'csv_sha256': sha(worker), 'rows': len(rows), 'parser_stats': worker_stats,
            'desktop_stats': desktop_stats, 'identical_csv_bytes': True,
            'identical_training_input': True, 'compatibility_rows_validated': len(rows),
            'seconds_three_parses': round(time.perf_counter()-started, 3),
        })
    report = {'parser': PARSER_VERSION, 'environment': {
        'python': sys.version, 'platform': platform.platform(),
        'demoparser2': importlib.metadata.version('demoparser2'),
    }, 'tests': results, 'model_training_performed': False,
        'split_hash': None, 'reason': 'parser parity only; no model selection or final-test evaluation',
        'gsi_alignment_verified': False, 'programme_coverage_verified': False}
    report_path = args.out / 'validation.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'report': str(report_path), 'rows': sum(item['rows'] for item in results)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
