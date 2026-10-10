"""安全的基础计算：白名单 ast 求值（非 eval），供 calculate skill 调用。

模型输出不可信，所以不用裸 eval，只放行白名单里的运算符/函数。
算术：+ - * / // % **、括号、列表、元组。
函数：sqrt/pow/abs/round/sum/min/max/floor/ceil、
     mean/average/median/stdev/pstdev（统计）、
     product/factorial（连乘/阶乘）、
     log/log10/exp/sin/cos/tan、常量 pi/e。
"""
from __future__ import annotations

import ast
import math
import statistics
from typing import Any

# 允许的二元运算符
_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

# 允许的一元运算符
_UNARYOPS = {
    ast.USub: lambda a: -a,
    ast.UAdd: lambda a: +a,
}

# 允许的函数
_FUNCS = {
    # 基础 / 聚合
    "sqrt": math.sqrt, "pow": math.pow, "abs": abs, "round": round,
    "sum": sum, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil,
    # 统计
    "mean": statistics.mean, "average": statistics.mean, "median": statistics.median,
    "stdev": statistics.stdev, "pstdev": statistics.pstdev,
    # 连乘 / 阶乘
    "product": math.prod, "factorial": math.factorial,
    # 对数 / 指数 / 三角
    "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    # 常量
    "pi": lambda: math.pi, "e": lambda: math.e,
}


def safe_calc(expression: str) -> str:
    """安全计算一个数学表达式，返回结果字符串。白名单之外的语法抛 ValueError。"""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("表达式不能为空")

    tree = ast.parse(expression.strip(), mode="eval")

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
            return _UNARYOPS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.List):
            return [_eval(e) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval(e) for e in node.elts)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
                fn = _FUNCS[node.func.id]
                args = [_eval(a) for a in node.args]
                kwargs = {kw.arg: _eval(kw.value) for kw in node.keywords if kw.arg}
                return fn(*args, **kwargs)
            raise ValueError(f"不允许的函数调用: {ast.dump(node)}")
        if isinstance(node, ast.Name):
            if node.id in ("pi", "e"):
                return _FUNCS[node.id]()
            raise ValueError(f"不允许的标识符: {node.id}")
        raise ValueError(f"不支持的语法: {ast.dump(node)}")

    try:
        return str(_eval(tree.body))
    except ZeroDivisionError:
        raise ValueError("除零错误")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"计算失败: {exc}")
