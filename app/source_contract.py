"""来源契约与字段级收货事实。

地磅补传只承担重量字段（毛重、皮重、单位或直接净重），化验复验只承担
水杂字段（水分、杂质）。同一个车次下，各字段分别按“提供该字段的最高
版本”裁决，而不是整行取最高版本：两边各自的有效信息可以合成一张事实，
每个字段又都保留来源行、版本与原回执摘要。

约束：
- 车次（trip）是入仓身份；没有车次列时取磅单号。地磅原地重试不会产生
  第二车次。
- 事实的磅单号取最早一张地磅/完整回执的磅单号，重试不改变原磅单身份。
- 同版同字段出现不同值，整组隔离，绝不挑一个。
- 合并后仍缺字段的回执记为待续跑，不落事实；补传到达后按车次续跑。
- 水分超过收购上限整组拒收，不落事实。
"""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import unicodedata

from app.errors import DomainError
from app.normalize import SENTINELS, number
from app.reasons import REASONS, rejected

FACTORS = {'kg': 1, 't': 1000}

SCALE_FIELDS = ('gross', 'tare', 'unit', 'net_kg')
LAB_FIELDS = ('moisture', 'impurity')
VALUE_FIELDS = ('bin', 'gross', 'tare', 'unit', 'net_kg', 'moisture', 'impurity')
NUMERIC_FIELDS = ('gross', 'tare', 'net_kg', 'moisture', 'impurity')

SCALE_ALIASES = {
    'scale', 'weighbridge', 'weigh', 'weight', 'wb', 'wbridge',
    '地磅', '地磅补传', '磅单', '过磅', '称重', '毛重',
}
LAB_ALIASES = {
    'lab', 'laboratory', 'test', 'retest', 'recheck', 'quality', 'q',
    '化验', '复验', '化验复验', '检验', '质检', '品质',
}
FULL_ALIASES = {
    'full', 'legacy', 'both', 'complete', 'original', 'all',
    '完整', '原始', '磅单化验', '全量',
}
SOURCE_LABEL = {'scale': '地磅', 'lab': '化验', 'full': '完整回执'}
SOURCE_FIELDS = {
    'scale': set(SCALE_FIELDS),
    'lab': set(LAB_FIELDS),
    'full': set(SCALE_FIELDS) | set(LAB_FIELDS),
}
SOURCE_COLUMNS = ('source', 'src', 'receipt_source', '数据来源', '来源', '来源类型', '采集来源')
TRIP_COLUMNS = ('trip', 'trip_id', 'trip_no', '车次', '车次号', '运单号')
VEHICLE_COLUMNS = ('vehicle', 'plate', 'plate_no', 'license', '车牌', '车牌号')
# 一辆车同一时间只能卸一车：没有车次列时，用“车牌 + 磅单时间”兜底。
VEHICLE_TIME_COLUMNS = ('weigh_time', 'time', 'timestamp', '过磅时间', '磅单时间')

DERIVED_FIELDS = {
    'net_kg': '（毛重-皮重）×单位换算',
    'clean_kg': '净重×（1-杂质/100）',
    'dry_kg': '净粮×（1-水分/100）',
    'standard_kg': '干物质/（1-标准水分/100）',
}
WEIGHT_INPUTS = ('gross', 'tare', 'unit')


def text(value):
    return unicodedata.normalize('NFKC', str(value)).strip()


def present(row, key):
    if key not in row:
        return False
    value = text(row[key])
    return value != '' and value.upper() not in SENTINELS and value not in SENTINELS


def carried(row, key):
    """字段是否出现在回执中（空单元格表示本来源未携带，不算携带）。"""
    return key in row and text(row[key]) != ''


def declared_source(row):
    for key in SOURCE_COLUMNS:
        if key in row and text(row[key]) != '':
            return text(row[key])
    return ''


def source_of(row):
    """返回 scale / lab / full；未登记或无法判断时返回 None。"""
    token = declared_source(row).lower()
    if token:
        if token in SCALE_ALIASES:
            return 'scale'
        if token in LAB_ALIASES:
            return 'lab'
        if token in FULL_ALIASES:
            return 'full'
        return None
    # 未声明来源时按字段携带情况识别：
    # - 重量与水杂都有 -> 完整回执；
    # - 只有重量 -> 地磅回执；
    # - 只有水杂 -> 化验回执；
    # - 重量行里带哨兵水杂（如 NULL）不是“未携带”，而是完整回执缺值，
    #   仍按完整回执校验并隔离。
    has_weight = any(present(row, key) for key in SCALE_FIELDS)
    has_lab = any(present(row, key) for key in LAB_FIELDS)
    lab_marked = any(carried(row, key) for key in LAB_FIELDS)
    if has_weight and (has_lab or lab_marked):
        return 'full'
    if has_weight:
        return 'scale'
    if has_lab:
        return 'lab'
    return None


def weight_complete(provided):
    """净重可由毛重、皮重、单位算出，也可由地磅直接给出。"""
    return 'net_kg' in provided or set(WEIGHT_INPUTS) <= set(provided)


def allowed_fields(kind):
    """本来源可承担的字段。

    bin 是仓位元数据，化验行可以回带，但只用于与地磅回执核对一致性，
    不参与字段级取值，避免复验把车次挪仓。
    """
    return set(SOURCE_FIELDS[kind]) | {'bin'}


def trip_of(row):
    for key in TRIP_COLUMNS:
        if key in row and text(row[key]) != '':
            return text(row[key])
    vehicle = next((text(row[key]) for key in VEHICLE_COLUMNS
                    if key in row and text(row[key]) != ''), '')
    moment = next((text(row[key]) for key in VEHICLE_TIME_COLUMNS
                   if key in row and text(row[key]) != ''), '')
    if vehicle and moment:
        return '%s@%s' % (vehicle, moment)
    return text(row.get('id', ''))


def raw_fields(row):
    return {k: str(v) for k, v in row.items() if not k.startswith('_')}


def receipt_key(row):
    """回执的稳定键：车次 + 磅单号 + 来源 + 版本 + 原文摘要。

    重试行若内容完全一致会得到同一键，用于去重；版本不同则保留为不同
    回执，由字段级裁决决定是否替换。
    """
    payload = {
        'trip': trip_of(row),
        'id': text(row.get('id', '')),
        'source': source_of(row) or text(declared_source(row)).lower(),
        'version': text(row.get('version', '')),
        'raw': raw_fields(row),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _line_key(line):
    return line if isinstance(line, int) else 10 ** 12


def _winner_key(row):
    """同版同值时优先保留早已登记的原回执，其次按原行号。"""
    return (_line_key(row['_line']), 0 if row.get('_origin') == 'prior' else 1, row['_key'])


def _canonical(value):
    if isinstance(value, Decimal):
        return value.normalize()
    return text(value).lower()


def _missing(selected):
    missing = []
    if 'bin' not in selected:
        missing.append('bin')
    if not weight_complete(selected):
        for key in WEIGHT_INPUTS:
            if key not in selected:
                missing.append(key)
    for key in ('moisture', 'impurity'):
        if key not in selected:
            missing.append(key)
    return missing


def parse_receipt(row):
    """校验并归一一张回执。返回内部行，异常为 DomainError。"""
    row = dict(row)
    line = row.get('_line', '')
    if not trip_of(row):
        raise DomainError('MISSING', '车次与磅单号均为空')
    if not present(row, 'version'):
        raise DomainError('MISSING', '版本为空')
    try:
        version = number(row['version'])
    except DomainError:
        raise
    if version != version.to_integral() or version < 1:
        raise DomainError('RANGE', '版本必须为正整数')
    version = int(version)
    kind = row.get('_source') if row.get('_source') in SOURCE_LABEL else source_of(row)
    if kind is None:
        raise DomainError('UNMAPPED', '来源或字段不属于已登记契约')
    allowed = allowed_fields(kind)
    foreign = [key for key in VALUE_FIELDS if key not in allowed and present(row, key)]
    if foreign:
        raise DomainError('SOURCE', '来源契约不符：' + ','.join(sorted(foreign)))

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

    # 完整回执必须一次带齐。部分来源允许不完整：缺的字段留到合并阶段
    # 按车次补齐，补齐不了的车次记待续跑，而不是直接丢掉回执。
    if kind == 'full':
        missing = [key for key in ('bin', 'moisture', 'impurity') if key not in provided]
        if not weight_complete(provided):
            missing.extend(key for key in WEIGHT_INPUTS if key not in provided)
        if missing:
            raise DomainError('MISSING', '完整回执缺少' + ','.join(missing))
    elif not (provided & (set(SOURCE_FIELDS[kind]) - {'unit'})):
        raise DomainError('MISSING', '没有可采信的来源字段')

    gross, tare = values.get('gross'), values.get('tare')
    if gross is not None and tare is not None:
        if gross < tare or tare < 0:
            raise DomainError('RANGE', '毛皮重越界')
    if values.get('moisture') is not None and not 0 <= values['moisture'] < 100:
        raise DomainError('RANGE', '水分越界')
    if values.get('impurity') is not None and not 0 <= values['impurity'] < 100:
        raise DomainError('RANGE', '杂质越界')

    if {'gross', 'tare', 'unit'} <= provided:
        values['net_kg'] = (gross - tare) * FACTORS[values['unit']]
        provided.add('net_kg')
    elif 'net_kg' in provided:
        values['net_kg'] = values['net_kg']

    stored = row.get('_raw_fields') if isinstance(row.get('_raw_fields'), dict) else None
    if stored:
        stored = {k: v for k, v in stored.items() if not k.startswith('_')}
    return {
        **row,
        '_line': line,
        '_source': kind,
        '_version': version,
        '_trip': trip_of(row),
        '_provided': provided,
        '_values': values,
        '_raw_fields': dict(stored) if stored else raw_fields(row),
        '_key': row.get('_key') or receipt_key(row),
        '_origin': row.get('_origin', 'current'),
        '_resumed': bool(row.get('_resumed')),
    }


def reject_receipt(row, code, detail="", inactive=False):
    """隔离一条原始回执，并带上回执键便于台账停用。

    inactive 只标记不可信的那张回执（同版矛盾的一方、越权带字段的一方），
    同车次的其他回执仍保留，便于修正后的补传继续合并。
    """
    entry = rejected(row, code, detail)
    entry['receipt'] = row.get('_key', '')
    entry['trip'] = row.get('_trip', '') or trip_of(row)
    entry['source'] = row.get('_source', '')
    entry['inactive'] = bool(inactive)
    return entry


def receipt_rejection(summary, code, detail=""):
    """对已合成事实中的原回执开隔离记录，保留原行号与原磅单号。"""
    return {
        'line': summary.get('line', ''),
        'key': summary.get('id', ''),
        'code': code,
        'reason': REASONS.get(code, code),
        'detail': detail,
        'origin': summary.get('origin', 'current'),
        'source': summary.get('source', ''),
        'trip': summary.get('trip', ''),
        'raw': summary.get('raw', {}),
    }


def _summary(row, contributed, selected_fields):
    return {
        'key': row['_key'],
        'line': row.get('_line', ''),
        'id': text(row.get('id', '')),
        'trip': row['_trip'],
        'source': row['_source'],
        'source_label': SOURCE_LABEL.get(row['_source'], row['_source']),
        'version': row['_version'],
        'origin': row.get('_origin', 'current'),
        'fields': dict(sorted(((k, row['_raw_fields'][k]) for k in row['_provided'] if k in row['_raw_fields']))),
        'selected_fields': sorted(selected_fields),
        'contributed': contributed,
        'raw': row['_raw_fields'],
        'digest': hashlib.sha256(json.dumps(row['_raw_fields'], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def _identity_row(group):
    """原磅单身份行：最早一张地磅或完整回执；台账里早已登记的原回执优先。"""
    weigh = [r for r in group if r['_source'] in ('scale', 'full')]
    pool = weigh or group
    return min(pool, key=lambda r: (0 if r.get('_origin') == 'prior' else 1,
                                    r['_version'], _line_key(r['_line']), r['_key']))


def _receipt_id(group):
    """原磅单身份：最早一张地磅或完整回执的磅单号。"""
    return text(_identity_row(group)['id'])


def _pending_entry(trip, group, missing):
    identity = text(_identity_row(group)['id'])
    return {
        'trip': trip,
        'id': identity,
        'missing': missing,
        'lines': [r.get('_line', '') for r in group],
        'receipts': [r['_key'] for r in group],
        'status': '待续跑',
    }


def resolve(rows):
    """把回执按来源契约合并为收货事实。

    返回 (facts, rejected, pending, receipts)。pending 记录合并后仍缺字段
    的车次，其回执同时计入隔离，保证行数平衡；补传到达后可继续。
    """
    parsed, bad = [], []
    for row in rows:
        try:
            parsed.append(parse_receipt(row))
        except DomainError as exc:
            entry = rejected(row, exc.code, exc.message)
            entry['receipt'] = row.get('_key') or receipt_key(row)
            entry['trip'] = trip_of(row)
            entry['inactive'] = (row.get('_origin', 'current') == 'current'
                                 and exc.code in ('SOURCE', 'UNMAPPED'))
            bad.append(entry)
    groups = defaultdict(list)
    seen = {}
    for row in parsed:
        identity = (row['_trip'], row['_key'])
        if identity in seen:
            # 台账里已有的同一条回执被本次输入再次带回时静默复用，
            # 不重复计入隔离；同一批输入内部的完全重复才隔离。
            if row.get('_origin') != 'prior':
                bad.append(rejected(row, 'DUPLICATE', '相同回执重复'))
            continue
        seen[identity] = row
        groups[row['_trip']].append(row)
    facts, pending = [], []
    for trip, group in sorted(groups.items()):
        group = sorted(group, key=lambda r: (_line_key(r['_line']), r['_version'],
                                             0 if r.get('_origin') == 'prior' else 1, r['_key']))
        candidates = {}
        for field in VALUE_FIELDS:
            cands = [r for r in group if field in r['_provided']]
            if field == 'bin':
                # 仓号只采信地磅/完整回执；化验行回带的仓号仅做一致性核对。
                weigh = [r for r in cands if r['_source'] in ('scale', 'full')]
                cands = weigh
            if cands:
                candidates[field] = cands
        conflict, conflicting = None, set()
        for field, cands in candidates.items():
            highest = max(r['_version'] for r in cands)
            top = [r for r in cands if r['_version'] == highest]
            if len({_canonical(r['_values'][field]) for r in top}) > 1:
                conflict = field
                conflicting = {r['_key'] for r in top}
                break
        if conflict:
            bad.extend(reject_receipt(
                r, 'CONFLICT', '同版%s矛盾' % conflict,
                inactive=r.get('_origin') == 'current' and r['_key'] in conflicting)
                for r in group)
            continue

        selected, evidence, selected_receipts = {}, {}, {}
        for field, cands in candidates.items():
            highest = max(r['_version'] for r in cands)
            top = [r for r in cands if r['_version'] == highest]
            winner = min(top, key=_winner_key)
            selected[field] = winner['_values'][field]
            selected_receipts[field] = winner['_key']
            evidence[field] = {
                'receipt': winner['_key'],
                'line': winner['_line'],
                'version': winner['_version'],
                'origin': winner.get('_origin', 'current'),
                'source': winner['_source'],
                'source_label': SOURCE_LABEL.get(winner['_source'], winner['_source']),
                'raw_id': text(winner.get('id', '')),
                'value': winner['_raw_fields'].get(field, str(winner['_values'][field])),
            }

        # 化验行回带仓号时，必须与地磅仓号一致；不一致整组隔离。
        if 'bin' in selected:
            mismatched = [r for r in group if r['_source'] == 'lab'
                          and 'bin' in r['_provided']
                          and _canonical(r['_values']['bin']) != _canonical(selected['bin'])]
            if mismatched:
                guilty = {r['_key'] for r in mismatched}
                bad.extend(reject_receipt(
                    r, 'CONFLICT', '化验仓号与地磅不一致',
                    inactive=r.get('_origin') == 'current' and r['_key'] in guilty)
                    for r in group)
                continue

        missing = _missing(selected)
        if missing:
            pending.append(_pending_entry(trip, group, missing))
            bad.extend(reject_receipt(r, 'MISSING', '合并后缺少' + ','.join(missing))
                       for r in group if r.get('_origin') == 'current')
            continue

        # 净重证据：由毛重、皮重、单位计算时登记公式与输入字段。
        if set(WEIGHT_INPUTS) <= set(selected):
            evidence['net_kg'] = {
                'source': 'derived',
                'source_label': '计算',
                'formula': DERIVED_FIELDS['net_kg'],
                'inputs': list(WEIGHT_INPUTS),
                'receipt': '',
                'line': evidence.get('gross', {}).get('line', ''),
                'version': evidence.get('gross', {}).get('version', ''),
            }
        contributions = set(selected_receipts.values())
        winner_version = {f: evidence[f]['version'] for f in selected_receipts}
        summaries = []
        for r in group:
            fields = sorted(f for f, key in selected_receipts.items() if key == r['_key'])
            summaries.append(_summary(r, r['_key'] in contributions, fields))
            if r['_key'] not in contributions:
                mine = [f for f in r['_provided'] if f in winner_version]
                if any(winner_version[f] > r['_version'] for f in mine):
                    detail = '旧版字段被更高版本替换'
                else:
                    detail = '同版重复回执，保留最早行'
                bad.append(reject_receipt(r, 'DUPLICATE', detail))
        fact = {
            'id': _receipt_id(group),
            'ticket_id': _receipt_id(group),
            'trip': trip,
            'version': max(r['_version'] for r in group),
            '_line': min((r['_line'] for r in group if r['_key'] in contributions), key=_line_key),
            '_fact': True,
            '_receipt_keys': [r['_key'] for r in group],
            '_contributor_keys': sorted(contributions),
            '_sources': sorted({r['_source'] for r in group}),
            '_continuations': sum(1 for r in group if r.get('_origin') == 'prior'
                                  or r.get('_resumed')),
            '_receipts': summaries,
            'evidence': evidence,
        }
        for field in VALUE_FIELDS:
            if field in selected:
                fact[field] = selected[field]
        facts.append(fact)
    return facts, bad, pending, parsed


def merge(rows):
    """兼容直接调用：只返回 (facts, rejected)。"""
    facts, bad, _, _ = resolve(rows)
    return facts, bad


def attach_dry_matter(fact, rules):
    """根据合并事实计算净粮、干物质和折标重，并补派生证据。"""
    if fact['moisture'] > rules['max_moisture']:
        raise DomainError('LIMIT', '水分超过收购上限')
    net = fact['net_kg']
    clean = net * (1 - fact['impurity'] / Decimal(100))
    dry = clean * (1 - fact['moisture'] / Decimal(100))
    standard = dry / (1 - rules['standard_moisture'] / Decimal(100))
    out = dict(fact)
    out['net_kg'] = net
    out['clean_kg'] = clean
    out['dry_kg'] = dry
    out['standard_kg'] = standard
    evidence = dict(fact.get('evidence') or {})
    inputs = {
        'net_kg': list(WEIGHT_INPUTS),
        'clean_kg': ['net_kg', 'impurity'],
        'dry_kg': ['clean_kg', 'moisture'],
        'standard_kg': ['dry_kg', 'standard_moisture'],
    }
    for field, formula in DERIVED_FIELDS.items():
        if field == 'net_kg' and not set(WEIGHT_INPUTS) <= set(fact):
            # 地磅直接给出净重时保留回执证据，不改成公式推导。
            continue
        base = evidence.get(field, {})
        evidence[field] = {
            'source': 'derived',
            'source_label': '计算',
            'formula': formula,
            'inputs': inputs[field],
            'receipt': '',
            'line': base.get('line', fact.get('_line', '')),
            'version': base.get('version', fact.get('version', '')),
        }
    out['evidence'] = evidence
    return out


def evidence_rows(facts, run_id=''):
    """导出字段来源：一行一个字段。"""
    out = []
    for fact in facts:
        for field in VALUE_FIELDS + ('clean_kg', 'dry_kg', 'standard_kg'):
            if field not in fact:
                continue
            ev = (fact.get('evidence') or {}).get(field, {})
            out.append({
                'run': run_id,
                'trip': fact.get('trip', ''),
                'id': fact.get('id', ''),
                'field': field,
                'value': str(fact.get(field, '')),
                'source': ev.get('source', ''),
                'source_label': ev.get('source_label', ''),
                'receipt': ev.get('receipt', ''),
                'raw_id': ev.get('raw_id', ''),
                'line': ev.get('line', ''),
                'version': ev.get('version', ''),
                'formula': ev.get('formula', ''),
                'inputs': ','.join(ev.get('inputs', []) or []),
            })
    return out
