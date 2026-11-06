"""Frozen, content-bound group partitions. Final test never participates in selection."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math


class SplitError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class SplitPlan:
    dataset_hash: str
    mode: str
    train: tuple
    validation: tuple
    final_test: tuple
    cv: tuple
    version: int = 1

    @property
    def split_hash(self):
        return digest(asdict(self))

    def manifest(self):
        return dict(asdict(self), split_hash=self.split_hash)

    @classmethod
    def from_manifest(cls, value):
        plan = cls(value['dataset_hash'], value['mode'], tuple(value['train']),
                   tuple(value['validation']), tuple(value['final_test']),
                   tuple((tuple(a), tuple(b)) for a, b in value['cv']), value['version'])
        if plan.version != 1 or plan.split_hash != value['split_hash']:
            raise SplitError('切分清单版本或摘要不匹配')
        return plan


def dataset_hash(xs, ys, groups, times):
    return digest([xs, ys, groups, times])


def make_plan(xs, ys, groups, times, mode='auto', validation_size=.25):
    if mode not in ('auto', 'group', 'time', 'random'):
        raise SplitError('未知切分模式')
    # "random" remains a legacy alias for random GROUP ordering, never random rows.
    mode = 'time' if mode == 'time' else 'group'
    unique = sorted(set(groups))
    if len(unique) < 8:
        raise SplitError('至少需要8个独立来源/分组，禁止回退随机拆行；请补充可靠来源身份')
    if mode == 'time':
        if any(not math.isfinite(t) or t <= 0 for t in times):
            raise SplitError('时间切分要求全部样本有可靠正时间戳')
        first = {}
        for t, g in zip(times, groups):
            first[g] = min(t, first.get(g, t))
        unique.sort(key=lambda g: (first[g], g))
    else:
        unique.sort(key=lambda g: digest(['split-v1', 42, g]))
    final_n = max(1, math.ceil(len(unique) * .2))
    val_n = max(1, math.ceil((len(unique) - final_n) * validation_size))
    train_g = unique[:-final_n-val_n]
    val_g = unique[-final_n-val_n:-final_n]
    final_g = unique[-final_n:]
    def indices(gs):
        selected = set(gs)
        return tuple(i for i, g in enumerate(groups) if g in selected)
    train, val, final = map(indices, (train_g, val_g, final_g))
    def check(a, b):
        if not a or not b or len({ys[i] for i in a}) < 2 or len({ys[i] for i in b}) < 2:
            raise SplitError('切分后类别或分组不足；禁止回退有泄漏的随机拆行')
        if set(groups[i] for i in a) & set(groups[i] for i in b):
            raise SplitError('检测到组交集')
        if mode == 'time' and max(times[i] for i in a) >= min(times[i] for i in b):
            raise SplitError('时间分组窗口重叠，无法保证仅用过去预测未来')
    check(train, val); check(train + val, final)
    cv = []
    for fold in range(3):
        if mode == 'time':
            blocks = [train_g[i*len(train_g)//4:(i+1)*len(train_g)//4] for i in range(4)]
            a, b = indices(sum(blocks[:fold+1], [])), indices(blocks[fold+1])
        else:
            b = indices(train_g[fold::3])
            excluded = set(b)
            a = tuple(i for i in train if i not in excluded)
        check(a, b)
        lookup = {row: i for i, row in enumerate(train)}
        cv.append((tuple(lookup[i] for i in a), tuple(lookup[i] for i in b)))
    return SplitPlan(dataset_hash(xs, ys, groups, times), mode, train, val, final, tuple(cv))


def validate_plan(plan, xs, ys, groups, times):
    if plan.dataset_hash != dataset_hash(xs, ys, groups, times):
        raise SplitError('数据内容与冻结切分不一致')
    # Recreate and compare structural invariants even for externally supplied plans.
    partitions = [plan.train, plan.validation, plan.final_test]
    flat = sum((list(p) for p in partitions), [])
    if sorted(flat) != list(range(len(ys))):
        raise SplitError('切分必须无重复且覆盖全部样本')
    if len(plan.cv) != 3 or any(len({ys[i] for i in p}) != 2 for p in partitions):
        raise SplitError('切分缺少有效二分类样本或三折内层验证')
    gs = [set(groups[i] for i in p) for p in partitions]
    if any(gs[a] & gs[b] for a, b in ((0, 1), (0, 2), (1, 2))):
        raise SplitError('冻结切分有组泄漏')
    for a, b in plan.cv:
        if not a or not b or set(a) & set(b) or any(i < 0 or i >= len(plan.train) for i in a+b):
            raise SplitError('无效内层交叉验证')
        aa, bb = [plan.train[i] for i in a], [plan.train[i] for i in b]
        if set(groups[i] for i in aa) & set(groups[i] for i in bb):
            raise SplitError('内层交叉验证有组泄漏')
        if plan.mode == 'time' and max(times[i] for i in aa) >= min(times[i] for i in bb):
            raise SplitError('内层交叉验证时间泄漏')
    if plan.mode == 'time' and any(max(times[i] for i in a) >= min(times[i] for i in b)
                                    for a, b in zip(partitions, partitions[1:])):
        raise SplitError('冻结切分时间泄漏')
