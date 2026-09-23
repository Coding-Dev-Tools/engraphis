"""Bounded interpreter for coding-corpus candidate expressions.

This file also runs standalone in the campaign container. Candidate text is
data: it is never imported, compiled, or passed to exec/eval. The interpreter
exposes primitive values and explicit operations, never Python runtime objects.
This is a deliberately small task language, not a sandbox for arbitrary Python.
"""
from __future__ import annotations

import ast
import inspect
import json
import operator
from pathlib import Path
import sys


CONTRACT = "engraphis-candidate-expressions/v1"
RESULT_MARKER = "__ENGRAPHIS_ORACLE_RESULT__"
MAX_SOURCE_BYTES = 65536
MAX_NODES = 4096
MAX_STEPS = 10000
MAX_DEPTH = 32
MAX_VALUE_SIZE = 8192
MAX_FRAME_BYTES = 8192
_UNBOUND = object()
_BUILTINS = {"str": str, "int": int, "float": float, "bool": bool, "len": len}
_METHODS = {str: {"strip", "casefold", "lower", "startswith"}, dict: {"get"}}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.Is: operator.is_, ast.IsNot: operator.is_not,
            ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_NODES = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign,
          ast.Return, ast.If, ast.Expr, ast.Pass, ast.Name, ast.Load, ast.Store,
          ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Call, ast.keyword,
          ast.Attribute, ast.IfExp, ast.Compare, ast.BoolOp, ast.And, ast.Or,
          ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BinOp, ast.Subscript)


class CandidateContractError(ValueError):
    """The candidate is outside the versioned language or resource contract."""


class _Return(Exception):
    def __init__(self, value):
        self.value = value


def _bounded(value, depth=0, remaining=None):
    if remaining is None:
        remaining = [MAX_VALUE_SIZE]
    remaining[0] -= len(value) if type(value) in (str, bytes) else 1
    if remaining[0] < 0:
        raise CandidateContractError("aggregate value size limit")
    if depth > MAX_DEPTH:
        raise CandidateContractError("value nesting limit")
    if type(value) in (str, bytes, list, tuple, dict) and len(value) > MAX_VALUE_SIZE:
        raise CandidateContractError("value size limit")
    if type(value) is int and value.bit_length() > MAX_VALUE_SIZE:
        raise CandidateContractError("integer size limit")
    if type(value) in (list, tuple):
        for item in value:
            _bounded(item, depth + 1, remaining)
    elif type(value) is dict:
        for key, item in value.items():
            _bounded(key, depth + 1, remaining)
            _bounded(item, depth + 1, remaining)
    elif value is not None and type(value) not in (str, bytes, int, float, bool):
        raise CandidateContractError("non-primitive value")
    return value


class _Interpreter:
    def __init__(self, source):
        if len(source) > MAX_SOURCE_BYTES:
            raise CandidateContractError("source size limit")
        try:
            tree = ast.parse(source.decode("utf-8"), filename="service.py")
        except (UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
            raise CandidateContractError("invalid source") from exc
        nodes = list(ast.walk(tree))
        if len(nodes) > MAX_NODES:
            raise CandidateContractError("syntax size limit")
        top_functions = {id(node) for node in tree.body if isinstance(node, ast.FunctionDef)}
        call_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        call_names.update(_BUILTINS)
        call_names.add("print")
        for node in nodes:
            if not isinstance(node, _NODES + tuple(_BINARY) + tuple(_COMPARE)):
                raise CandidateContractError("unsupported syntax: " + type(node).__name__)
            if isinstance(node, (ast.Name, ast.arg)):
                name = node.id if isinstance(node, ast.Name) else node.arg
                if name.startswith("__"):
                    raise CandidateContractError("reserved name")
            if isinstance(node, ast.FunctionDef) and (
                id(node) not in top_functions or node.decorator_list or node.returns is not None
                or node.name.startswith("__") or node.args.vararg or node.args.kwarg
                or any(arg.annotation is not None for arg in (
                    node.args.posonlyargs + node.args.args + node.args.kwonlyargs))
            ):
                raise CandidateContractError("unsupported function declaration")
            if isinstance(node, ast.Assign) and any(not isinstance(t, ast.Name) for t in node.targets):
                raise CandidateContractError("assign only to local names")
            if isinstance(node, ast.Call) and (
                not isinstance(node.func, (ast.Name, ast.Attribute))
                or any(keyword.arg is None for keyword in node.keywords)
                or (isinstance(node.func, ast.Name) and node.func.id not in call_names)
            ):
                raise CandidateContractError("unsupported call")
            if isinstance(node, ast.Attribute) and node.attr not in {"strip", "casefold", "lower", "startswith", "get"}:
                raise CandidateContractError("unsupported attribute")
            if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
                raise CandidateContractError("dictionary unpacking is unsupported")
            if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Is, ast.IsNot)) and not (
                    isinstance(value, ast.Constant) and (value.value is None or type(value.value) is bool))
                for op, value in zip(node.ops, node.comparators)
            ):
                raise CandidateContractError("identity comparisons require None or a boolean literal")
        self.globals = {}
        self.functions = {}
        self.steps = 0
        self.depth = 0
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if node.name in self.globals or node.name in self.functions or node.name in _BUILTINS or node.name == "print":
                    raise CandidateContractError("duplicate or reserved definition")
                positional = node.args.posonlyargs + node.args.args
                defaults = [inspect.Parameter.empty] * (len(positional) - len(node.args.defaults))
                defaults += [self.expr(value, self.globals) for value in node.args.defaults]
                params = [inspect.Parameter(arg.arg, inspect.Parameter.POSITIONAL_ONLY
                          if i < len(node.args.posonlyargs) else inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=defaults[i]) for i, arg in enumerate(positional)]
                params += [inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                           default=inspect.Parameter.empty if value is None else self.expr(value, self.globals))
                           for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults)]
                try:
                    signature = inspect.Signature(params)
                except ValueError as exc:
                    raise CandidateContractError("invalid parameters") from exc
                self.functions[node.name] = (node, signature)
            elif isinstance(node, (ast.Assign, ast.Expr, ast.Pass)):
                self.statement(node, self.globals)
            else:
                raise CandidateContractError("unsupported module statement")

    def tick(self):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise CandidateContractError("operation limit")

    def call(self, name, args, kwargs):
        self.tick()
        _bounded([args, kwargs])
        if name == "print":
            if set(kwargs) - {"sep", "end"}:
                raise CandidateContractError("print supports only sep and end")
            print(*args, **kwargs, file=sys.stderr)
            return None
        if name in _BUILTINS:
            return _bounded(_BUILTINS[name](*args, **kwargs))
        if name not in self.functions:
            raise NameError(name)
        node, signature = self.functions[name]
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        env = {target.id: _UNBOUND for child in ast.walk(node) if isinstance(child, ast.Assign)
               for target in child.targets}
        env.update(bound.arguments)
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CandidateContractError("call depth limit")
        try:
            for statement in node.body:
                self.statement(statement, env)
        except _Return as result:
            return result.value
        finally:
            self.depth -= 1
        return None

    def statement(self, node, env):
        self.tick()
        if isinstance(node, ast.Return):
            raise _Return(None if node.value is None else self.expr(node.value, env))
        if isinstance(node, ast.Assign):
            value = self.expr(node.value, env)
            for target in node.targets:
                if target.id in self.functions or target.id in _BUILTINS or target.id == "print":
                    raise CandidateContractError("reserved assignment")
                env[target.id] = value
        elif isinstance(node, ast.Expr):
            self.expr(node.value, env)
        elif isinstance(node, ast.If):
            for child in node.body if self.expr(node.test, env) else node.orelse:
                self.statement(child, env)
        elif not isinstance(node, ast.Pass):
            raise CandidateContractError("unsupported statement")

    def expr(self, node, env):
        self.tick()
        return _bounded(self._expr(node, env))

    def _expr(self, node, env):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                if env[node.id] is _UNBOUND:
                    raise UnboundLocalError(node.id)
                return env[node.id]
            if node.id in self.globals:
                return self.globals[node.id]
            raise NameError(node.id)
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self.expr(value, env) for value in node.elts]
            return tuple(values) if isinstance(node, ast.Tuple) else values
        if isinstance(node, ast.Dict):
            return {self.expr(key, env): self.expr(value, env) for key, value in zip(node.keys, node.values)}
        if isinstance(node, ast.Subscript):
            return self.expr(node.value, env)[self.expr(node.slice, env)]
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.expr(node.test, env) else node.orelse, env)
        if isinstance(node, ast.UnaryOp):
            value = self.expr(node.operand, env)
            return {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}[type(node.op)](value)
        if isinstance(node, ast.BoolOp):
            value = self.expr(node.values[0], env)
            for child in node.values[1:]:
                if (isinstance(node.op, ast.And) and not value) or (isinstance(node.op, ast.Or) and value):
                    break
                value = self.expr(child, env)
            return value
        if isinstance(node, ast.Compare):
            left = self.expr(node.left, env)
            for op, child in zip(node.ops, node.comparators):
                right = self.expr(child, env)
                if not _COMPARE[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BinOp):
            left, right = self.expr(node.left, env), self.expr(node.right, env)
            if isinstance(node.op, ast.Mult):
                for sequence, count in ((left, right), (right, left)):
                    if type(sequence) in (str, bytes, list, tuple) and type(count) in (int, bool):
                        if len(sequence) * max(count, 0) > MAX_VALUE_SIZE:
                            raise CandidateContractError("sequence size limit")
            if isinstance(node.op, ast.Mod) and type(left) in (str, bytes):
                raise CandidateContractError("string formatting is unsupported")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.Call):
            args = [self.expr(value, env) for value in node.args]
            kwargs = {keyword.arg: self.expr(keyword.value, env) for keyword in node.keywords}
            if len(kwargs) != len(node.keywords):
                raise CandidateContractError("duplicate keyword")
            if isinstance(node.func, ast.Name):
                if node.func.id in env or node.func.id in self.globals:
                    raise CandidateContractError("calling value aliases is unsupported")
                return self.call(node.func.id, args, kwargs)
            receiver = self.expr(node.func.value, env)
            if node.func.attr not in _METHODS.get(type(receiver), set()):
                raise CandidateContractError("unsupported method for value type")
            return getattr(receiver, node.func.attr)(*args, **kwargs)
        raise CandidateContractError("unsupported expression")


def evaluate_candidate(source, function_name, args, kwargs):
    """Evaluate one primitive operation under the explicit candidate contract."""
    _bounded(args)
    _bounded(kwargs)
    return _Interpreter(source).call(function_name, args, kwargs)


def _json_value(value):
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return value == value and value not in (float("inf"), float("-inf"))
    if type(value) is list:
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    return False


def main():
    try:
        path = Path("service.py")
        if path.is_symlink() or not path.is_file():
            raise CandidateContractError("service.py must be a regular file")
        try:
            with path.open("rb") as source:
                data = source.read(MAX_SOURCE_BYTES + 1)
        except OSError as exc:
            raise CandidateContractError("cannot read service.py") from exc
        value = evaluate_candidate(data, sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3]))
        if not _json_value(value):
            raise TypeError("candidate result must be JSON-compatible")
        payload = {"ok": True, "value": value}
        serialized = json.dumps(payload, sort_keys=True, allow_nan=False)
        if len((RESULT_MARKER + serialized + "\n").encode("utf-8")) > MAX_FRAME_BYTES:
            raise CandidateContractError("result size limit")
    except (CandidateContractError, MemoryError, RecursionError) as exc:
        payload = {"ok": False, "contract_error": True, "error_type": type(exc).__name__}
        serialized = json.dumps(payload, sort_keys=True)
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__}
        serialized = json.dumps(payload, sort_keys=True)
    print(RESULT_MARKER + serialized, flush=True)


if __name__ == "__main__":
    main()
