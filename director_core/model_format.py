"""Bounded v1 JSON forest validation, independent of sklearn and desktop IO."""
import math

DECISION_FEATURES = ['aim_self', 'aim_enemy', 'win', 'enemy_low_hp',
                     'self_low_hp', 'proximity', 'enemy_damaged', 'hp_advantage']


def forest_probability(model, features):
    total = 0.0
    for tree in model['trees']:
        i = 0
        while tree['left'][i] != -1:
            i = tree['left'][i] if features[tree['feature'][i]] <= tree['threshold'][i] else tree['right'][i]
        negative, positive = tree['value'][i]
        total += positive / (negative + positive)
    return total / len(model['trees'])


def validate_forest(data):
    if data.get('n_features') != 8 or data.get('features') != DECISION_FEATURES:
        raise ValueError('模型特征名称/顺序不匹配')
    if data.get('feature_version') != 'pov-v1' or data.get('classes') != [0, 1]:
        raise ValueError('模型版本/类别不兼容')
    trees = data.get('trees')
    if not isinstance(trees, list) or not 1 <= len(trees) <= 800:
        raise ValueError('模型树数量无效')
    total = 0
    for tree in trees:
        n = len(tree['left'])
        total += n
        if not n or total > 2000000 or any(len(tree[k]) != n for k in ('right', 'feature', 'threshold', 'value')):
            raise ValueError('模型节点预算或数组不一致')
        stack, seen = [(0, 0)], set()
        while stack:
            i, depth = stack.pop()
            if type(i) is not int or i < 0 or i >= n or i in seen or depth > 64:
                raise ValueError('模型循环/索引/深度无效')
            seen.add(i)
            value = tree['value'][i]
            if len(value) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in value) or sum(value) <= 0:
                raise ValueError('模型叶值无效')
            if not math.isfinite(tree['threshold'][i]):
                raise ValueError('模型阈值无效')
            left, right = tree['left'][i], tree['right'][i]
            if left == -1 and right == -1:
                continue
            if type(tree['feature'][i]) is not int or not 0 <= tree['feature'][i] < 8:
                raise ValueError('模型特征索引无效')
            stack.extend([(left, depth + 1), (right, depth + 1)])
        if len(seen) != n:
            raise ValueError('模型包含不可达节点')
    return data
