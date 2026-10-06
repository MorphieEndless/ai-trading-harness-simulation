#!/usr/bin/env python3
"""trader 包的静态检查：抓「用了但没定义的变量」。

为什么需要这个
-------------
这个项目没有 CI，代码是构建时烤进镜像的。一个拼错的变量名不会被 import 抓到
（Python 只在真正执行到那一行时才报 NameError），于是它会安安静静地进镜像，
然后在**某一轮心跳里**才炸 —— 表现为调度器每 30 秒重试一次、每次都失败、
日志里刷满 traceback，而面板上只显示"有错误"。

真实案例（这个测试就是为了它写的）：
    把 self._last_narrated_id = bus.seq 写进了 _beat()，
    而 bus 只存在于 __init__ 的作用域里。
    py_compile 通过、import 通过、测试通过，直到线上跑第一轮心跳才报
    NameError: name 'bus' is not defined。

这里做的事很朴素：把每个模块 AST 走一遍，收集所有被"读取"的名字，
再和该作用域链上所有被"绑定"的名字（参数、赋值、import、for 目标、
with/except 别名、推导式变量、global/nonlocal）+ 内置函数对账。
对不上就报。

它不是 pyflakes，不做流分析，会漏掉一些真问题；但上面那类错它必抓。
不联网，不花钱。

跑法：bash runtests.sh lint
"""
from __future__ import annotations

import ast
import builtins
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def _find_trader() -> pathlib.Path:
    for cand in (ROOT / "brain" / "trader", ROOT / "trader"):
        if (cand / "agent.py").is_file():
            return cand
    raise SystemExit(f"找不到 trader 包（找过 {ROOT}/brain/trader 和 {ROOT}/trader）")


BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "self", "cls",  # 兜底，正常情况应该由参数绑定捕获
}


class Scope:
    """一层作用域（模块 / 函数 / lambda / 类）。"""

    def __init__(self, node, parent: "Scope | None" = None):
        self.node = node
        self.parent = parent
        self.bound: set[str] = set()
        self.globals: set[str] = set()      # global X
        self.nonlocals: set[str] = set()    # nonlocal X


def bind_target(node, scope: Scope) -> None:
    """把一个赋值/循环目标里的名字都绑进作用域。

    必须处理节点**本身**，而不只是它的子节点 —— 因为
        for t in tools
    里的 t 就是一个裸 Name，它的子节点是空的。
    推导式的目标（{... for m in models}）也是同样的情况。

    ★ 必须检查 ctx：只有 Store / Del 才是"绑定"。
      漏了这个检查，read 位置的名字也会被当成已定义 ——
        self._last_narrated_id: int = bus.seq
      里的 bus（Load）就成了"已绑定"，于是全检查器变成摆设。
      （这个 bug 被自己抓到过一次，见文件头那段说明。）
    """
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            scope.bound.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            bind_target(elt, scope)
    elif isinstance(node, ast.Starred):
        bind_target(node.value, scope)


def collect_bindings(node, scope: Scope) -> None:
    """把这个作用域里所有"绑定名字"的地方记下来（不递归进子作用域）。"""
    # 节点自身可能就是个绑定目标
    bind_target(node, scope)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 定义本身在当前作用域绑定名字；函数体内部单独处理
            scope.bound.add(child.name)
            for d in child.decorator_list:
                collect_bindings(d, scope)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 默认值 / 注解在**外层**作用域求值
                for a in (list(child.args.args) + list(child.args.posonlyargs)
                          + list(child.args.kwonlyargs)):
                    if a.annotation is not None:
                        collect_bindings(a.annotation, scope)
                    if getattr(a, "default", None) is not None:
                        collect_bindings(a.default, scope)
                if child.args.vararg and child.args.vararg.annotation is not None:
                    collect_bindings(child.args.vararg.annotation, scope)
                if child.args.kwarg and child.args.kwarg.annotation is not None:
                    collect_bindings(child.args.kwarg.annotation, scope)
                if child.returns is not None:
                    collect_bindings(child.returns, scope)
                for d in child.args.defaults:
                    collect_bindings(d, scope)
                for d in child.args.kw_defaults:
                    if d is not None:
                        collect_bindings(d, scope)
            continue
        if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                              ast.GeneratorExp)):
            continue  # 自己的作用域，由 _push 负责
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            scope.bound.add(child.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                scope.bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ExceptHandler):
            # except X as name —— 这里的 name 是**字符串**，不是 Name 节点，
            # 所以必须单独处理，否则每个 except ... as exc 都会误报。
            if child.name:
                scope.bound.add(child.name)
        elif isinstance(child, ast.Global):
            scope.globals.update(child.names)
        elif isinstance(child, ast.Nonlocal):
            scope.nonlocals.update(child.names)
        collect_bindings(child, scope)


class Checker(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.problems: list[str] = []
        self.scopes: list[Scope] = []

    # ------------------------------------------------------------------
    def _push(self, node) -> Scope:
        parent = self.scopes[-1] if self.scopes else None
        sc = Scope(node, parent)
        # 先把直接子节点里的绑定收集一遍（不做流分析，够用）
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if isinstance(node, ast.Lambda):
                args = node.args
            else:
                args = node.args
            for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)):
                sc.bound.add(a.arg)
            if args.vararg:
                sc.bound.add(args.vararg.arg)
            if args.kwarg:
                sc.bound.add(args.kwarg.arg)
            if not isinstance(node, ast.Lambda):
                collect_bindings(node, sc)
        elif isinstance(node, (ast.Module, ast.ClassDef)):
            collect_bindings(node, sc)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                bind_target(gen.target, sc)
        self.scopes.append(sc)
        return sc

    def _pop(self) -> None:
        self.scopes.pop()

    def _resolve(self, name: str) -> bool:
        sc = self.scopes[-1] if self.scopes else None
        seen = set()
        while sc is not None:
            if name in sc.globals:
                break
            if name in sc.bound:
                return True
            seen.add(id(sc))
            sc = sc.parent
        # 模块级
        for s in self.scopes:
            if isinstance(s.node, ast.Module) and name in s.bound:
                return True
        return name in BUILTINS

    # ------------------------------------------------------------------
    def visit_Module(self, node):
        self._push(node)
        self.generic_visit(node)
        self._pop()

    def _visit_func(self, node):
        self._push(node)
        self.generic_visit(node)
        self._pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Lambda(self, node):
        self._push(node)
        self.generic_visit(node)
        self._pop()

    def visit_ClassDef(self, node):
        self._push(node)
        self.generic_visit(node)
        self._pop()

    def _visit_comp(self, node):
        self._push(node)
        self.generic_visit(node)
        self._pop()

    visit_ListComp = _visit_comp
    visit_SetComp = _visit_comp
    visit_DictComp = _visit_comp
    visit_GeneratorExp = _visit_comp

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and not self._resolve(node.id):
            self.problems.append(
                f"{self.filename}:{node.lineno} 未定义的名字 `{node.id}`"
            )
        self.generic_visit(node)


def check_file(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path.name}:{exc.lineno} 语法错误：{exc.msg}"]
    c = Checker(path.name)
    c.visit(tree)
    return c.problems


def main() -> int:
    trader = _find_trader()
    files = sorted(trader.glob("*.py"))
    print("=" * 68)
    print(f" 静态检查：未定义的名字（{len(files)} 个模块）")
    print("=" * 68)

    all_problems: list[str] = []
    for f in files:
        probs = check_file(f)
        if probs:
            print(f"\n  ✗ {f.name}")
            for p in probs:
                print(f"      {p}")
            all_problems.extend(probs)
        else:
            print(f"  ✓ {f.name}")

    print()
    print("=" * 68)
    if all_problems:
        print(f" 发现 {len(all_problems)} 处可疑引用")
        print(" （如果确认是误报——比如动态赋值——就在 test_lint.py 里加白名单）")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
