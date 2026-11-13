"""Offline, complete-source training plans. Never submits data or changes wire v1."""
from collections import OrderedDict
import csv
import json
from pathlib import Path
import shutil
import tempfile
import uuid

import dataset_protocol as p
from dataset_export import csv_rows, export_csv, sha_file
from device_identity import atomic_write

TARGET_ROWS = 180000
RESERVE = 512 * 1024**2


def check_disk(directory, additional):
    directory = Path(directory).resolve()
    while not directory.exists():
        directory = directory.parent
    if shutil.disk_usage(directory).free < additional + RESERVE:
        raise ValueError(f'磁盘空间不足：本阶段至少需要约 {(additional+RESERVE)/1024**3:.2f} GiB 可用空间，请更换输出目录。')


def preflight_demos(values, output, log=lambda text: None):
    from train_from_demos import expand_demo_paths
    paths = expand_demo_paths([str(v) for v in values])
    if not paths:
        raise ValueError('没有找到 .dem 文件，请检查目录或文件选择。')
    size = sum(path.stat().st_size for path in paths)
    # This is a preparation budget, not a claim that DEM bytes predict CSV size.
    check_disk(output, size * 2)
    log(f'目录预检：{len(paths)} 个 Demo，原件 {size/1024**3:.2f} GiB；按原件两倍加 512 MiB 检查临时空间。解析后再按实际样本检查。')
    return paths


def _source(row):
    if all(row.get(key) not in (None, '') for key in p.EXTRA):
        p.pattern(row['match_source_id'], p.HEX)
        return row['match_source_id']
    if any(row.get(key) not in (None, '') for key in p.EXTRA):
        raise ValueError('来源元数据不完整，请从原 Demo 重新解析；未自动拆分或补造来源。')
    return 'legacy'


def prepare_batches(path, destination, *, target_rows=TARGET_ROWS, check_space=lambda size: None,
                    log=lambda text: None):
    """Persist every batch before training; never split a recording or candidate group.

    Each batch is checked by the actual frozen-v1 exporter before any model fit.
    Training models are independent candidates, not an ensemble or a global model.
    """
    path, destination = Path(path), Path(destination)
    p.require(1 <= target_rows <= p.LIMITS['maxRows'], 'ROW_LIMIT')
    size = path.stat().st_size
    p.require(size > 0, 'NO_SAMPLES')
    check_space(size * 2 + p.LIMITS['maxCompressedBytes'])
    check_disk(destination, size * 4)
    before = sha_file(path)
    sources = OrderedDict()
    columns = None
    for row in csv_rows(path):
        if columns is None: columns = list(row)
        key = _source(row)
        item = sources.setdefault(key, {'rows': 0, 'bytes': 0})
        item['rows'] += 1
        # Upper budget includes normalized group hashes and CSV quoting.
        item['bytes'] += sum(len(str(v).encode('utf-8')) for v in row.values()) * 2 + len(row) * 3 + 256
        if item['rows'] > p.LIMITS['maxRows']:
            raise ValueError('单个完整来源超过 200000 行，无法按现有协议提交。请提高采样间隔重新解析；旧 CSV 需补齐来源。不会截断来源。')
    p.require(bool(sources), 'NO_SAMPLES')
    p.require(not ('legacy' in sources and len(sources) > 1), 'MIXED_ROW_SCHEMAS')
    groups, current, rows, budget = [], [], 0, 0
    for key, item in sources.items():
        if current and (rows + item['rows'] > target_rows or len(current) >= p.LIMITS['maxSources']
                        or budget + item['bytes'] > 64*1024**2):
            groups.append(current);current, rows, budget = [], 0, 0
        current.append(key);rows += item['rows'];budget += item['bytes']
    if current: groups.append(current)
    # Reserve retained immutable inputs plus one in-flight package; worker remains serial.
    check_space(size * 2 + len(groups) * p.LIMITS['maxCompressedBytes'])
    destination.mkdir(parents=True, exist_ok=False)
    mapping = {key: index for index, group in enumerate(groups) for key in group}
    paths = [destination / f'batch-{i+1:04d}.csv' for i in range(len(groups))]
    for output in paths:
        with output.open('x', encoding='utf-8', newline='') as stream:
            csv.DictWriter(stream, fieldnames=columns).writeheader()
    handles = OrderedDict()
    try:
        for row in csv_rows(path):
            index = mapping[_source(row)]
            if index not in handles:
                if len(handles) >= 16: handles.popitem(last=False)[1][0].close()
                stream = paths[index].open('a', encoding='utf-8', newline='')
                handles[index] = stream, csv.DictWriter(stream, fieldnames=columns)
            handles.move_to_end(index)
            handles[index][1].writerow(row)
    finally:
        for stream, _ in handles.values(): stream.close()
    if len(paths) == 1:
        # Preserve the exact original byte snapshot for an unsplit training run.
        shutil.copyfile(path, paths[0])
    p.require(sha_file(path) == before, 'INPUT_CHANGED')
    batches = []
    for index, (output, group) in enumerate(zip(paths, groups), 1):
        log(f'校验批次 {index}/{len(groups)}：{sum(sources[k]["rows"] for k in group)} 行，{len(group)} 个完整来源')
        with tempfile.TemporaryDirectory(prefix='.validate-', dir=destination) as temporary:
            try:
                descriptor = export_csv(output, Path(temporary)/'package', str(uuid.uuid4()))
                verified_rows = descriptor['manifest']['totals']['rows']
                upload_ready = True
            except p.Invalid:
                if group != ['legacy']: raise
                # Existing local CSV training remains usable even when provenance
                # is insufficient for upload. The durable package error explains it.
                verified_rows = sources['legacy']['rows']
                upload_ready = False
                log('旧 CSV 可继续本地训练；当前样本不满足上传校验，成功后将在队列保留整理原因。')
        batches.append({'csv': str(output.resolve()), 'sha256': sha_file(output),
                        'rows': verified_rows, 'sources': len(group), 'uploadReady': upload_ready,
                        'sourceKind': 'legacy' if group == ['legacy'] else 'demo-derived'})
    plan = {'schema': 'director-local-batch-plan-v1', 'inputSha256': before,
            'targetRows': target_rows, 'batches': batches,
            'note': '各批独立候选；全部来源保持完整。接收后仍需人工筛选、主动训练及独立发布。'}
    atomic_write(destination/'plan.json', json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8'))
    return plan


def legacy_notice(source_kind):
    return ('\n\n此输入缺少当前 Demo 来源元数据，按旧格式处理；来源需管理员核实。可从原 Demo 重新解析。'
            if source_kind == 'legacy' else '')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='离线规划完整来源训练批次，不训练或上传')
    parser.add_argument('--csv', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--target-rows', type=int, default=TARGET_ROWS)
    args = parser.parse_args()
    plan = prepare_batches(args.csv, args.out, target_rows=args.target_rows, log=print)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
