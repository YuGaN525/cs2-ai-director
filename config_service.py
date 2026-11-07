"""Pure validation of editable settings, independent of Tk and services."""
import math


FIELDS = {
    'gsi_port': ('GSI端口', 1024, 65535, int),
    'obs_port': ('OBS端口', 1, 65535, int),
    'switch_min_score': ('切换阈值', .05, .95, float),
    'manual_pause_seconds': ('人工接管秒数', 0, 120, float),
}


class ConfigError(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def parse_fields(values):
    result = {}
    for key, raw in values.items():
        label, low, high, converter = FIELDS[key]
        try:
            value = converter(str(raw).strip())
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ConfigError(key, f'{label}必须为{low}至{high}之间的有效数字') from None
        result[key] = value
    return result
