"""字段别名表 —— 全部从真实帖子的统计里长出来，不是设计出来的。

key 一律先经 `normalize_key()` 剥掉 emoji 装饰再查表，所以这里只写素面写法。
括号里的数字是该写法在真实数据中的出现次数，用于判断优先级和是否可以删。
`漫画 N` 是 ACG Library 漫画频道导出（2026-10-24，938 帖）里的次数 —— 两个频道
同一个 up 主、同一套模板（动漫频道甚至转发过几条漫画帖），所以共用这一张表。

改动原则：帖子已经发出去了，改不了。新增写法只往这里加，不动解析代码。
"""

from __future__ import annotations

import re

# 剥掉 key 两侧的 emoji / 符号 / 空白，只留中日文、拉丁字母、数字
_KEY_JUNK = re.compile(r"[^0-9A-Za-z一-鿿぀-ヿー]+")


def normalize_key(raw: str) -> str:
    """'☺️评分' -> '评分'; '🩶GoogleDrive' -> 'GoogleDrive'; '⛓️解压' -> '解压'"""
    return _KEY_JUNK.sub("", raw)


# ---------------------------------------------------------------- 元信息字段
# canonical -> 真实写法。解析时反查成 alias -> canonical 的扁平表。
META_ALIASES: dict[str, tuple[str, ...]] = {
    "title_cn":     ("中文名", "名字"),                       # 1446 + 9 / 漫画 836 + 117
    "title_en":     ("英文名",),                              # 114
    "title_alias":  ("别名",),                                # 5 / 漫画 765
    "episodes":     ("话数",),                                # 1481 / 漫画 507
    # 「开始」对番剧是放送开始，对漫画是连载开始 —— 都是作品起点，落同一个字段
    "air_date":     ("放送开始", "上映年度", "上映",
                     "开始", "连载开始"),                      # 1361 + 14 / 漫画 372 + 89
    "end_date":     ("结束", "连载结束"),                      # 漫画 346 + 65
    "release_date": ("发售日",),                              # 漫画 900
    "air_weekday":  ("放送星期",),                            # 1214
    "duration":     ("片长",),                                # 6
    "score":        ("评分", "个人评分", "豆瓣评分", "黑猫简评"),  # 1483 + 6 / 漫画 265
    "tags":         ("标签",),                                # 1596 / 漫画 881
    "index":        ("引索", "索引"),                          # 1496 + 0 / 漫画 94 + 64
    "password":     ("解压", "解压码", "解压密码"),             # 1663
    "password_alt": ("如密码不对该换",),                        # 74 备用密码
    "extract_code": ("提取码",),                              # 21
    "file_size":    ("文件大小",),                            # 29
    "volumes":      ("册数",),                                # 3 / 漫画 520
    "magazine":     ("连载杂志",),                            # 漫画 515
    "edition":      ("版本",),                                # 漫画 310（多行，取第一行）
    "isbn":         ("ISBN", "ISBN10"),                       # 漫画 269 + 73
    "pages":        ("页数",),                                # 漫画 194
    "price":        ("价格",),                                # 漫画 189
    "media_type":   ("类型",),                                # 漫画 109
    "imprint":      ("书系", "图书品牌", "文库", "Label"),      # 漫画 26 + 18 + 11 + 1
    "note":         ("备注", "其他"),                          # 漫画 28 + 16
}

# ---------------------------------------------------------------- 制作人员
# 全部塞进 Post.staff 这个 dict，不为每个职位开一列 —— 职位有 20 种且长尾。
STAFF_ALIASES: dict[str, tuple[str, ...]] = {
    "director":     ("导演",),                                # 1409
    "script":       ("脚本", "系列构成"),                      # 769 + 4
    "original":     ("原作", "作者", "插图", "漫画", "原案"),   # 766 / 漫画 196 + 624 + 45 + 8
    "art":          ("作画",),                                # 漫画 155
    "storyboard":   ("分镜",),                                # 617
    "performance":  ("演出",),                                # 31
    "music":        ("音乐", "音楽", "音乐目录"),               # 47
    "studio":       ("制作", "製作", "动画制作",
                     "アニメーション制作", "企画"),              # 38
    "chara_design": ("人物设定", "人物原案", "美术监督"),        # 15 / 漫画 44
    "voice":        ("配音监督", "声优"),                      # 4
    "producer":     ("监制", "企划制作人", "制作人"),            # 7
    "publisher":    ("出版社", "其他出版社"),                   # 4 / 漫画 780 + 20
}

# ---------------------------------------------------------------- 资源链接
# 同一网盘的 emoji 前缀有 7 种变体，剥离后才收敛。
# `link` 是「链接：」这种不说指向哪里的写法：种类由 URL 主机定（domain.links.kind_of_url），
# 漫画频道的「链接: Bangumi」因此落成 bangumi，动漫频道的「链接：pan.baidu…」落成 baidu。
LINK_ALIASES: dict[str, tuple[str, ...]] = {
    "baidu":    ("百度网盘", "百度", "百度下载", "百度秒传",
                 "秒传", "秒传见表格"),                        # 1511 + 122
    "onedrive": ("OneDrive", "辅助网盘"),                     # 1104 + 204
    "gdrive":   ("GoogleDrive", "GD", "谷歌网盘"),            # 124 + 9 + 61
    "aliyun":   ("阿里网盘", "阿里", "阿里下载", "阿里4K"),      # 119
    "quark":    ("夸克网盘",),                                # 29
    "lanzou":   ("蓝奏", "蓝奏云"),                            # 1 / 漫画 3 + 2
    "yandex":   ("Yandex",),                                 # 漫画 3
    "online":   ("在线观看",),                                # 漫画 46（t.me 在线阅读频道）
    "link":     ("链接",),                                    # 7 / 漫画 788
    "sheet":    ("往期番剧汇总表格", "往期番剧汇总", "信息页面"),  # 1188 + 25
    "raw_disk": ("原盘链接",),                                # 3
    "official": ("官方网站", "官网"),                          # 3 / 漫画 3 + 2
}

# 正文里出现但不是字段的行首词，命中即跳过，避免污染 extra。
# 「链接」曾经在这里；它现在是 LINK_ALIASES 的 `link`，按主机定种类。
NOISE_KEYS: frozenset[str] = frozenset({
    "https", "http", "PS", "Re", "tg", "t", "引用",   # 引用: 作者简介（漫画 9）
})

# 按钮文字 -> 链接类型。按钮只有 129 个帖子有，但文字比正文 key 干净。
BUTTON_ALIASES: dict[str, str] = {
    "百度链接": "baidu", "百度网盘": "baidu", "百度": "baidu",
    "谷歌表格": "sheet", "秒传见表格": "sheet",
    "谷歌网盘": "gdrive",
    "OD节点": "od_node", "CDN节点": "cdn_node",
}

# 全频道公用的汇总表格，不入库到单条帖子（1964 次 + 90 次）
SHARED_SHEET_IDS: frozenset[str] = frozenset({
    "1q2JyP3A4lok0jhkN-Tl3N3D5uo8uDJFX",
    "1dkruirMvu78hvf6JA9sL2ADvdQe7dnjT0xwOsIFhp44",
})


def _flatten(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, aliases in groups.items():
        for a in aliases:
            out[normalize_key(a)] = canonical
    return out


META_LOOKUP = _flatten(META_ALIASES)
STAFF_LOOKUP = _flatten(STAFF_ALIASES)
LINK_LOOKUP = _flatten(LINK_ALIASES)
