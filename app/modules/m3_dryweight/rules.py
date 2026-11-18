"""折标计算：在合并后的收货事实上按干物质折标。

净粮 = 净重 × (1 - 杂质/100)
干物质 = 净粮 × (1 - 水分/100)
折标重 = 干物质 / (1 - 标准水分/100)

标准水分与收购上限来自批次参数，本模块只读取不修改。
"""
from decimal import Decimal as D

from app.errors import DomainError
from app.source_contract import attach_dry_matter


def convert(row, rules):
    """对一张已带净重的完整记录做折标；保留独立调用入口。"""
    if row['moisture'] > rules['max_moisture']:
        raise DomainError('LIMIT', '水分超过收购上限')
    out = dict(row)
    out['clean_kg'] = row['net_kg'] * (1 - row['impurity'] / D(100))
    out['dry_kg'] = out['clean_kg'] * (1 - row['moisture'] / D(100))
    out['standard_kg'] = out['dry_kg'] / (1 - rules['standard_moisture'] / D(100))
    return out


def fold(fact, rules):
    return attach_dry_matter(fact, rules)
