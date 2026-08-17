"""Case configuration for the Yanlian refinery scheduling model.

Edit this file when the case data, modeling scope, mappings, or objective
weights change. The Gurobi model implementation imports these settings from
`refinery_scheduling_gurobi.py`.
"""

CASE_WORKBOOK = "延炼延石化_储罐与装置侧线与原油_数据自整理_0724.xlsx"
REPORT_PATTERN = "延安炼油厂装置生产完成情况表*.xlsx"
DEFAULT_DAYS_IN_REPORT_MONTH = 31

DEFAULT_HORIZON_DAYS = 7
DEFAULT_YCO_SHARE = 0.55

CRUDES = ("YCO", "RCO")
CDU_UNITS = ("300万常压装置", "260万常压装置")

SELECTED_TANK_MATERIALS = {
    "原油",
    "1#渣油",
    "常压石脑油",
    "常压直馏柴油",
    "航煤原料",
    "催化汽油",
    "催化柴油",
    "液化气",
    "精制液化气",
    "C4",
    "醚后C4",
    "甲醇原料",
    "醋酸原料",
    "MTBE",
    "醋酸仲丁酯",
    "酯后碳四",
    "乙苯",
    "烃化液",
    "脱氢液",
    "苯乙烯",
    "石油苯",
    "丙烯",
    "丙烷",
    "0#柴油",
    "92#汽油",
    "95#汽油",
    "3#喷气燃料",
}

UNIT_NAME_MAP = {
    "300万常压装置": "300万常压装置",
    "260万常压装置": "260万常压装置",
    "30万吨液化气精制装置": "液化气精制装置",
    "30万吨气体分馏装置": "气体分馏装置",
    "10万吨聚丙烯装置": "聚丙烯装置",
    "6万吨MTBE装置": "MTBE装置",
    "7万吨醋酸仲丁酯装置": "醋酸仲丁酯装置",
    "50万吨航煤加氢装置": "50万航煤加氢装置",
    "18万吨干气精制装置": "干气精制装置",
    "12万吨乙苯装置": "乙苯装置200单元",
    "12万吨苯乙烯装置300单元": "苯乙烯装置300单元",
    "12万吨苯乙烯装置400单元": "苯乙烯装置400单元",
    "催化装置合计": "",
    "常压装置合计": "",
}

CDU_SIDE_NAME_MAP = {
    "WN1": "初常顶油",
    "NK1": "石脑油(重整原料)",
    "KE1": "常一线(喷气燃料)",
    "LD1": "常二线(柴油)",
    "HD1": "常三线(VGO)",
    "VR1": "减压渣油",
}

ASSAY_PROPERTY_BY_SECTION = {
    "*比重": "SPG",
    "*硫含量": "SUL",
    "*康氏残炭": "CON",
    "*辛烷值": "RON",
    "*抗爆指数": "DON",
    "*芳烃含量": "ARW",
    "*切割温度": "CUT_TEMP",
}

EXCLUDED_REPORT_ROW_KEYWORDS = (
    "合计",
    "其中",
    "原料处理量",
    "自用",
    "掺炼",
    "冲减后",
    "单位领导",
    "报出日期",
    "异丁烯转化率",
    "苯乙烯收率",
)

REPORT_MATERIAL_RULES = (
    (("酸性水", "酸性气", "瓦斯", "尾气", "干气"), "燃料气/酸性气"),
    (("原油",), "原油"),
    (("1#渣油", "渣油"), "1#渣油"),
    (("常压石脑油", "石脑油Ⅲ", "石脑油产量", "石脑油"), "常压石脑油"),
    (("航煤组分油", "3#喷气燃料", "常一线"), "航煤原料"),
    (("常二线", "常三线", "常压直馏柴油"), "常压直馏柴油"),
    (("催化柴油",), "催化柴油"),
    (("汽油",), "催化汽油"),
    (("精制液化气",), "精制液化气"),
    (("液化气",), "液化气"),
    (("醚后C4",), "醚后C4"),
    (("酯后C4", "酯后碳四"), "酯后碳四"),
    (("碳四", "C4"), "C4"),
    (("甲醇",), "甲醇原料"),
    (("醋酸仲丁酯", "SBA"), "醋酸仲丁酯"),
    (("醋酸",), "醋酸原料"),
    (("MTBE",), "MTBE"),
    (("聚丙烯",), "聚丙烯"),
    (("丙烯",), "丙烯"),
    (("丙烷",), "丙烷"),
    (("乙苯",), "乙苯"),
    (("苯乙烯",), "苯乙烯"),
    (("脱氢液",), "脱氢液"),
    (("烃化液",), "烃化液"),
    (("苯",), "石油苯"),
)

EXTERNAL_SOURCE_MATERIALS = {
    "YCO",
    "RCO",
    "甲醇原料",
    "醋酸原料",
    "石油苯",
    "干气",
}

OBJECTIVE_WEIGHTS = {
    "load_deviation": 1000.0,
    "unit_switch": 100.0,
    "inventory_change": 1.0,
    "external_source": 25.0,
    "shipment": 0.005,
    "load_variation": 1.0,
}

# Collaborative Yanlian--Yanshihua model configuration.  The legacy baseline
# ignores this section and remains a Yanlian-only case.
PLANTS = ("延炼", "延石化")

UNIT_PLANTS = {
    "300万常压装置": "延炼",
    "260万常压装置": "延炼",
    "200万催化装置": "延炼",
    "100万催化装置": "延炼",
    "液化气精制装置": "延炼",
    "气体分馏装置": "延炼",
    "聚丙烯装置": "延炼",
    "MTBE装置": "延炼",
    "醋酸仲丁酯装置": "延炼",
    "50万航煤加氢装置": "延炼",
    "干气精制装置": "延炼",
    "乙苯装置200单元": "延炼",
    "苯乙烯装置300单元": "延炼",
    "苯乙烯装置400单元": "延炼",
    "120万重整装置": "延石化",
    "140万柴油加氢装置": "延石化",
    "20万苯抽提装置": "延石化",
    "60万气体分馏装置": "延石化",
    "20万聚丙烯装置": "延石化",
    "12万MTBE装置": "延石化",
    "60万精制装置": "延石化",
    "硫磺回收装置": "延石化",
    "硫磺精制装置": "延石化",
    "180万汽油精制装置": "延石化",
    "240万柴油加氢装置": "延石化",
    "20万烷基化装置": "延石化",
    "30万混合脱氢装置": "延石化",
    "25万MTBE装置": "延石化",
}

# Initial engineering limits for daily inter-plant movements (t/d).  They are
# deliberately finite and configurable; replace them with confirmed pipeline
# and truck-loading capacities when those records are available.
TRANSFER_CAPACITIES = {
    ("延炼", "延石化", "常压石脑油"): 5000.0,
    ("延炼", "延石化", "催化柴油"): 3500.0,
    ("延炼", "延石化", "常压直馏柴油"): 5000.0,
    ("延炼", "延石化", "催化汽油"): 5000.0,
    ("延炼", "延石化", "液化气"): 3000.0,
    ("延炼", "延石化", "C4"): 2500.0,
    ("延炼", "延石化", "丙烷"): 1500.0,
    ("延炼", "延石化", "醚后C4"): 2000.0,
    ("延石化", "延炼", "MTBE"): 2500.0,
    ("延石化", "延炼", "丙烯"): 1500.0,
    ("延石化", "延炼", "丙烷"): 1200.0,
    ("延石化", "延炼", "石油苯"): 1000.0,
    ("延石化", "延炼", "液化气"): 2000.0,
    ("延石化", "延炼", "醚后C4"): 2500.0,
    ("延石化", "延炼", "精制柴油"): 5000.0,
    ("延石化", "延炼", "精制汽油"): 5000.0,
}

# Plant-specific external boundaries.  External source is no longer available
# for every material; only declared purchases or unmodelled upstream streams
# can enter a plant node.
EXTERNAL_SOURCE_BY_PLANT = {
    "延炼": {"YCO", "RCO", "甲醇原料", "醋酸原料", "石油苯", "干气", "乙苯"},
    "延石化": {
        "常压石脑油", "催化柴油", "催化汽油", "液化气", "C4",
        "甲醇原料", "氢气", "轻烃", "酸性气", "饱和液化气",
        "戊烷", "精制液化气", "烷基化C4",
    },
}

SALEABLE_MATERIALS_BY_PLANT = {
    "延炼": {
        "0#柴油", "3#喷气燃料", "92#汽油", "95#汽油", "MTBE",
        "丙烯", "丙烷", "聚丙烯", "苯乙烯", "醋酸仲丁酯",
        "催化柴油", "催化汽油", "常压直馏柴油", "常压石脑油",
        "油浆", "焦炭", "液化气", "2#渣油", "石脑油Ⅲ", "石脑油",
        "航煤组分油", "甲苯", "酯后碳四",
    },
    "延石化": {
        "重整汽油", "重整轻汽油", "精制油", "精制柴油", "精制汽油",
        "粗汽油", "非芳烃", "MTBE", "聚丙烯", "烷基化油", "正丁烷",
        "精制硫磺", "石油苯", "丙烯", "丙烷", "液化气", "石脑油",
        "醚后C4",
    },
}

DISPOSABLE_MATERIALS = {
    "损失", "燃料气", "含硫燃料气", "干气", "烟道气", "污水",
    "污油", "轻污油", "OG气", "富气", "戊烷", "不合格液化气",
    "不合格丙烯", "次品MTBE", "次品SBA", "焦油", "C12", "丙苯",
    "烃化尾气", "脱氢尾气", "粗酯", "高沸物", "瓦斯气", "酸性水",
    "酸性气", "氢气", "净化干气",
}

# Working inventories for the main Yanshihua tanks.  Values are conservative
# engineering estimates from the supplied tank capacities/safe levels.  Rows
# without a current level use a mid-level initial inventory and are flagged by
# the collaborative model as assumptions.
YANSHIHUA_TANK_POOLS = {
    "常压石脑油": {"initial": 9000.0, "min_inventory": 2200.0, "max_inventory": 15600.0},
    "催化柴油": {"initial": 11000.0, "min_inventory": 4400.0, "max_inventory": 17800.0},
    "液化气": {"initial": 1650.0, "min_inventory": 660.0, "max_inventory": 2640.0},
    "丙烯": {"initial": 2200.0, "min_inventory": 880.0, "max_inventory": 3520.0},
    "丙烷": {"initial": 500.0, "min_inventory": 200.0, "max_inventory": 800.0},
    "C4": {"initial": 1100.0, "min_inventory": 440.0, "max_inventory": 1760.0},
    "MTBE": {"initial": 3500.0, "min_inventory": 1480.0, "max_inventory": 5920.0},
    "甲醇原料": {"initial": 2800.0, "min_inventory": 1200.0, "max_inventory": 5000.0},
    "烷基化油": {"initial": 3500.0, "min_inventory": 1380.0, "max_inventory": 5520.0},
    "轻污油": {"initial": 700.0, "min_inventory": 300.0, "max_inventory": 1200.0},
    "富异丁烯C4": {"initial": 1000.0, "min_inventory": 200.0, "max_inventory": 1800.0},
    "再生C4": {"initial": 400.0, "min_inventory": 100.0, "max_inventory": 800.0},
    "烷基化C4": {"initial": 800.0, "min_inventory": 100.0, "max_inventory": 1600.0},
}

COLLABORATIVE_OBJECTIVE_WEIGHTS = {
    **OBJECTIVE_WEIGHTS,
    "transfer": 0.05,
    "disposal": 200.0,
}
