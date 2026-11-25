"""An authored Blender project or shot plan needs a targeted correction."""


class EngineeringValidationError(ValueError):
    pass


def require_cycles(engine):
    if engine != "CYCLES":
        raise EngineeringValidationError("后台制作只使用 Cycles。请恢复 CYCLES，避免 Eevee 或 Workbench 初始化图形界面。")
