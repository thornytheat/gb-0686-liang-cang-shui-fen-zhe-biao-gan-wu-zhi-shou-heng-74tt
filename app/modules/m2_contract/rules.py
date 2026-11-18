"""水杂契约：按来源校核每张回执，而不是要求每行都带全字段。

地磅补传负责毛重、皮重、单位；化验复验负责水分、杂质。只有完整回执
（两者都带）才要求全部字段。标准水分与收购上限的边界保持不变。
"""
from app.normalize import number
from app.errors import DomainError
from app.source_contract import (
    FACTORS, VALUE_FIELDS, NUMERIC_FIELDS,
    allowed_fields, present, source_of, trip_of, text,
)

# 完整回执必须一次带齐；部分来源只要求至少携带本方一个字段，
# 其余字段留到合并阶段按车次补齐（缺字段的车次记待续跑）。
OWN_FIELDS = {'scale': ('gross', 'tare', 'unit', 'net_kg'), 'lab': ('moisture', 'impurity')}


def settings(params):
    out = {k: number(params[k]) for k in ['standard_moisture', 'max_moisture', 'price', 'bin_tolerance']}
    if not 0 <= out['standard_moisture'] < out['max_moisture'] < 100 or out['price'] < 0 or out['bin_tolerance'] < 0:
        raise DomainError('RANGE', '折标参数超出范围')
    return out


def coerce(row):
    """校验并归一一张回执；缺失字段按来源契约分别要求。"""
    kind = source_of(row)
    if kind is None:
        raise DomainError('UNMAPPED', '来源或字段不属于已登记契约')
    allowed = allowed_fields(kind)
    foreign = [key for key in VALUE_FIELDS if key not in allowed and present(row, key)]
    if foreign:
        raise DomainError('SOURCE', '来源契约不符：' + ','.join(sorted(foreign)))
    if kind == 'full':
        missing = [key for key in ('bin', 'moisture', 'impurity') if not present(row, key)]
        if not (present(row, 'net_kg') or all(present(row, k) for k in ('gross', 'tare', 'unit'))):
            missing.extend(k for k in ('gross', 'tare', 'unit') if not present(row, k))
        if missing:
            raise DomainError('MISSING', '缺少必填值：' + ','.join(missing))
    elif not any(present(row, key) for key in OWN_FIELDS[kind]):
        raise DomainError('MISSING', '没有可采信的来源字段')
    out = dict(row)
    values, provided = {}, set()
    for key in VALUE_FIELDS:
        if key not in allowed or not present(row, key):
            continue
        if key == 'bin':
            values[key] = text(row[key])
        elif key == 'unit':
            unit = text(row[key]).lower()
            if unit not in FACTORS:
                raise DomainError('UNIT', '重量单位无效')
            values[key] = unit
        else:
            values[key] = number(row[key])
        provided.add(key)
    gross, tare = values.get('gross'), values.get('tare')
    if gross is not None and tare is not None:
        if gross < tare or tare < 0:
            raise DomainError('RANGE', '毛皮重越界')
    if values.get('moisture') is not None and not 0 <= values['moisture'] < 100:
        raise DomainError('RANGE', '水分越界')
    if values.get('impurity') is not None and not 0 <= values['impurity'] < 100:
        raise DomainError('RANGE', '杂质越界')
    version = number(row['version'])
    if version != version.to_integral() or version < 1:
        raise DomainError('RANGE', '版本必须为正整数')
    version = int(version)
    for key in NUMERIC_FIELDS:
        if key in values:
            out[key] = values[key]
    if 'unit' in values:
        out['unit'] = values['unit']
    if 'bin' in values:
        out['bin'] = values['bin']
    out['version'] = version
    out['_source'] = kind
    out['_version'] = version
    out['_trip'] = trip_of(row)
    out['_provided'] = provided
    out['_values'] = values
    if {'gross', 'tare', 'unit'} <= provided:
        out['net_kg'] = (gross - tare) * FACTORS[values['unit']]
    return out
