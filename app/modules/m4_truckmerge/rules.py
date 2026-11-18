"""车次归并：先按来源契约把回执合成事实，再核对车次身份。

`merge` 保持回执级入口（供 M4 页面与规则测试直接调用），内部完全复用
来源契约；`audit_facts` 对已合成的事实做车次级校验。
"""
from collections import defaultdict

from app.reasons import rejected
from app.source_contract import merge as merge_receipts


def merge(rows):
    return merge_receipts(rows)


def audit_facts(rows):
    by_trip, by_id = defaultdict(list), defaultdict(list)
    for row in rows:
        by_trip[row.get('trip') or row.get('id', '')].append(row)
        by_id[str(row.get('id', ''))].append(row)
    good, bad = [], []
    for trip, group in sorted(by_trip.items()):
        if len(group) > 1:
            bad.extend(rejected(r, 'CONFLICT', '同一车次出现多张事实') for r in group)
            continue
        row = group[0]
        receipts = row.get('_receipts') or []
        if not receipts:
            bad.append(rejected(row, 'INCOMPLETE', '事实缺少原回执'))
            continue
        missing = [f for f in ('bin', 'net_kg', 'moisture', 'impurity') if f not in row]
        if missing:
            bad.append(rejected(row, 'INCOMPLETE', '事实缺少' + ','.join(missing)))
            continue
        good.append(row)
    for ticket, group in sorted(by_id.items()):
        if len({r.get('trip') for r in group}) > 1:
            for row in [r for r in group if any(r is g for g in good)]:
                good = [r for r in good if r is not row]
                bad.append(rejected(row, 'CONFLICT', '同一磅单号对应多个车次'))
    return good, bad


def ticket_identity(rows):
    """车次 -> 原磅单号，按证据顺序稳定。"""
    out = {}
    for row in rows:
        out.setdefault(row.get('trip') or row.get('id', ''), row.get('id', ''))
    return out
