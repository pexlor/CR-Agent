"""Deterministic interpreter for the finite review-tool rule language."""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.ports.tools import (
    AuthorizedToolInput,
    DeclarativeRule,
    FixedToolReference,
    RestrictedToolRuntimePort,
    ToolDeclaration,
    ToolEvidence,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolExecutionState,
    ToolLimits,
)

from .registry import ToolRegistry

INTERPRETER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class _RawMatch:
    rule_index: int
    rule: DeclarativeRule
    line: int
    column: int
    matched_value: str
    steps: int


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    kind: str
    line: int
    column: int


class _LimitExceeded(Exception):
    def __init__(self, limit_name: str) -> None:
        self.limit_name = limit_name


class _ExecutionBudget:
    def __init__(
        self,
        limits: ToolLimits,
        *,
        monotonic: Callable[[], float],
    ) -> None:
        self.limits = limits
        self.steps = 0
        self.matches = 0
        self.outputs = 0
        self._monotonic = monotonic
        self._deadline = monotonic() + (limits.soft_time_ms / 1000)

    def step(self, count: int = 1) -> None:
        self.steps += count
        if self.steps > self.limits.max_steps:
            raise _LimitExceeded("max_steps")
        if self._monotonic() > self._deadline:
            raise _LimitExceeded("soft_time_ms")

    def add_match(self) -> None:
        self.matches += 1
        if self.matches > self.limits.max_matches:
            raise _LimitExceeded("max_matches")
        self.outputs += 1
        if self.outputs > self.limits.max_output_items:
            raise _LimitExceeded("max_output_items")


class RestrictedToolRuntime(RestrictedToolRuntimePort):
    """Executes declarations without exposing imports, IO, processes or network."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._monotonic = monotonic

    def execute(
        self,
        fixed_tool_ref: FixedToolReference,
        authorized_input: AuthorizedToolInput,
        planned_limits: ToolLimits,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del execution_context
        declaration = self._registry.verify_fixed_reference(fixed_tool_ref)
        limits = declaration.limits.constrained_by(planned_limits)
        input_digest = authorized_input.input_digest

        normalization_error = self._validate_input(authorized_input, limits)
        if normalization_error is not None:
            return self._result(
                declaration=declaration,
                input_digest=input_digest,
                limits=limits,
                state=ToolExecutionState.BLOCKED,
                steps=0,
                evidence=(),
                error_code=normalization_error,
            )

        budget = _ExecutionBudget(limits, monotonic=self._monotonic)
        raw_matches: list[_RawMatch] = []
        try:
            for rule_index, rule in enumerate(declaration.rules):
                for match in self._execute_rule(
                    rule_index,
                    rule,
                    authorized_input,
                    budget,
                ):
                    budget.add_match()
                    raw_matches.append(match)
        except _LimitExceeded as error:
            return self._result(
                declaration=declaration,
                input_digest=input_digest,
                limits=limits,
                state=ToolExecutionState.FAILED_KNOWN,
                steps=budget.steps,
                evidence=(),
                error_code=f"tool_{error.limit_name}_exceeded",
            )

        ordered = sorted(
            raw_matches,
            key=lambda item: (
                item.line,
                item.column,
                item.rule_index,
                item.matched_value,
            ),
        )
        evidence = tuple(
            ToolEvidence(
                tool_id=declaration.tool_id,
                tool_version=declaration.version,
                rule_id=match.rule.rule_id,
                interpreter_version=INTERPRETER_VERSION,
                input_digest=input_digest,
                path=authorized_input.path,
                scope=authorized_input.scope,
                line=match.line,
                column=match.column,
                op=match.rule.op,
                op_params_digest=sha256_digest(dict(match.rule.params)),
                match_digest=sha256_digest(match.matched_value),
                steps=match.steps,
                ordinal=ordinal,
                message=match.rule.message,
            )
            for ordinal, match in enumerate(ordered, start=1)
        )
        return self._result(
            declaration=declaration,
            input_digest=input_digest,
            limits=limits,
            state=ToolExecutionState.SUCCEEDED,
            steps=budget.steps,
            evidence=evidence,
            error_code=None,
        )

    @staticmethod
    def _validate_input(
        authorized_input: AuthorizedToolInput, limits: ToolLimits
    ) -> str | None:
        if (
            "\r" in authorized_input.text
            or unicodedata.normalize("NFC", authorized_input.text)
            != authorized_input.text
        ):
            return "tool_input_not_normalized"
        if len(authorized_input.text.encode("utf-8")) > limits.max_input_bytes:
            return "tool_input_bytes_exceeded"
        if authorized_input.token_count > limits.max_tokens:
            return "tool_input_tokens_exceeded"
        if (
            len(authorized_input.path) > limits.max_field_length
            or len(authorized_input.scope) > limits.max_field_length
        ):
            return "tool_input_field_length_exceeded"
        return None

    @staticmethod
    def _result(
        *,
        declaration: ToolDeclaration,
        input_digest: str,
        limits: ToolLimits,
        state: ToolExecutionState,
        steps: int,
        evidence: tuple[ToolEvidence, ...],
        error_code: str | None,
    ) -> ToolExecutionResult:
        limits_digest = sha256_digest(limits.digest_payload())
        payload = {
            "state": state.value,
            "tool_id": declaration.tool_id,
            "tool_version": declaration.version,
            "interpreter_version": INTERPRETER_VERSION,
            "input_digest": input_digest,
            "limits_digest": limits_digest,
            "steps": steps,
            "error_code": error_code,
            "evidence": [
                {
                    "rule_id": item.rule_id,
                    "line": item.line,
                    "column": item.column,
                    "op": item.op,
                    "params": item.op_params_digest,
                    "match": item.match_digest,
                    "ordinal": item.ordinal,
                    "message": item.message,
                }
                for item in evidence
            ],
        }
        return ToolExecutionResult(
            state=state,
            tool_id=declaration.tool_id,
            tool_version=declaration.version,
            interpreter_version=INTERPRETER_VERSION,
            input_digest=input_digest,
            limits_digest=limits_digest,
            steps=steps,
            evidence=evidence,
            result_digest=sha256_digest(payload),
            error_code=error_code,
        )

    def _execute_rule(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        authorized_input: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        match rule.op:
            case "literal_contains":
                operation = self._op_literal_contains
            case "literal_not_contains":
                operation = self._op_literal_not_contains
            case "token_sequence":
                operation = self._op_token_sequence
            case "token_pair_within":
                operation = self._op_token_pair_within
            case "line_predicate":
                operation = self._op_line_predicate
            case "line_prefix":
                operation = self._op_line_prefix
            case "line_suffix":
                operation = self._op_line_suffix
            case "line_equals":
                operation = self._op_line_equals
            case "balanced_delimiter":
                operation = self._op_balanced_delimiter
            case "changed_line_only":
                operation = self._op_changed_line_only
            case "bounded_context_contains":
                operation = self._op_bounded_context_contains
            case "identifier_equals":
                operation = self._op_identifier_equals
            case "call_name_equals":
                operation = self._op_call_name_equals
            case "argument_literal_equals":
                operation = self._op_argument_literal_equals
            case _:  # Registry validation makes this unreachable.
                raise AssertionError("unsupported registered operation")
        yield from operation(rule_index, rule, authorized_input, budget)

    @staticmethod
    def _param_str(rule: DeclarativeRule, name: str) -> str:
        value = rule.params[name]
        assert isinstance(value, str)
        return value

    @staticmethod
    def _param_int(rule: DeclarativeRule, name: str) -> int:
        value = rule.params[name]
        assert isinstance(value, int)
        return value

    @staticmethod
    def _param_strings(rule: DeclarativeRule, name: str) -> tuple[str, ...]:
        value = rule.params[name]
        assert isinstance(value, tuple)
        return value

    @staticmethod
    def _lines(text: str) -> list[str]:
        return text.split("\n")

    @staticmethod
    def _offset_position(text: str, offset: int) -> tuple[int, int]:
        line = text.count("\n", 0, offset) + 1
        last_newline = text.rfind("\n", 0, offset)
        return line, offset - last_newline

    def _literal_matches(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        text: str,
        literal: str,
        budget: _ExecutionBudget,
        *,
        line_filter: frozenset[int] | None = None,
    ) -> Iterator[_RawMatch]:
        start = 0
        while start <= len(text):
            budget.step()
            offset = text.find(literal, start)
            if offset < 0:
                return
            line, column = self._offset_position(text, offset)
            if line_filter is None or line in line_filter:
                yield _RawMatch(rule_index, rule, line, column, literal, budget.steps)
            start = offset + max(1, len(literal))

    def _op_literal_contains(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        yield from self._literal_matches(
            rule_index,
            rule,
            value.text,
            self._param_str(rule, "literal"),
            budget,
        )

    def _op_literal_not_contains(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        literal = self._param_str(rule, "literal")
        budget.step()
        if literal not in value.text:
            yield _RawMatch(rule_index, rule, 1, 1, f"absent:{literal}", budget.steps)

    def _tokenize(self, text: str, budget: _ExecutionBudget) -> tuple[_Token, ...]:
        tokens: list[_Token] = []
        offset = 0
        line = 1
        column = 1
        while offset < len(text):
            budget.step()
            char = text[offset]
            if char == "\n":
                offset += 1
                line += 1
                column = 1
                continue
            if char.isspace():
                offset += 1
                column += 1
                continue
            start_line, start_column = line, column
            if char.isalpha() or char == "_":
                start = offset
                while offset < len(text) and (
                    text[offset].isalnum() or text[offset] == "_"
                ):
                    offset += 1
                    column += 1
                tokens.append(
                    _Token(text[start:offset], "identifier", start_line, start_column)
                )
                continue
            if char in ("'", '"'):
                quote = char
                offset += 1
                column += 1
                literal: list[str] = []
                while offset < len(text) and text[offset] != quote:
                    budget.step()
                    if text[offset] == "\\" and offset + 1 < len(text):
                        literal.append(text[offset + 1])
                        offset += 2
                        column += 2
                    else:
                        literal.append(text[offset])
                        offset += 1
                        column += 1
                if offset < len(text):
                    offset += 1
                    column += 1
                tokens.append(
                    _Token("".join(literal), "string", start_line, start_column)
                )
                continue
            tokens.append(_Token(char, "punctuation", start_line, start_column))
            offset += 1
            column += 1
        return tuple(tokens)

    def _op_token_sequence(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        expected = self._param_strings(rule, "tokens")
        tokens = self._tokenize(value.text, budget)
        for index in range(0, len(tokens) - len(expected) + 1):
            budget.step()
            candidate = tuple(
                token.value for token in tokens[index : index + len(expected)]
            )
            if candidate == expected:
                token = tokens[index]
                yield _RawMatch(
                    rule_index,
                    rule,
                    token.line,
                    token.column,
                    "\x1f".join(candidate),
                    budget.steps,
                )

    def _op_token_pair_within(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        first = self._param_str(rule, "first")
        second = self._param_str(rule, "second")
        maximum = self._param_int(rule, "max_tokens")
        tokens = self._tokenize(value.text, budget)
        for left_index, left in enumerate(tokens):
            if left.value != first:
                continue
            stop = min(len(tokens), left_index + maximum + 2)
            for right in tokens[left_index + 1 : stop]:
                budget.step()
                if right.value == second:
                    yield _RawMatch(
                        rule_index,
                        rule,
                        left.line,
                        left.column,
                        f"{first}\x1f{second}",
                        budget.steps,
                    )
                    break

    def _line_match(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
        *,
        mode: str,
        literals: tuple[str, ...],
    ) -> Iterator[_RawMatch]:
        for line_number, line in enumerate(self._lines(value.text), start=1):
            for literal in literals:
                budget.step()
                matches = (
                    line == literal
                    if mode == "equals"
                    else line.startswith(literal)
                    if mode == "prefix"
                    else line.endswith(literal)
                )
                if matches:
                    column = 1 if mode != "suffix" else len(line) - len(literal) + 1
                    yield _RawMatch(
                        rule_index,
                        rule,
                        line_number,
                        column,
                        literal,
                        budget.steps,
                    )

    def _op_line_predicate(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        for param_name, mode in (
            ("equals", "equals"),
            ("prefixes", "prefix"),
            ("suffixes", "suffix"),
        ):
            if param_name in rule.params:
                yield from self._line_match(
                    rule_index,
                    rule,
                    value,
                    budget,
                    mode=mode,
                    literals=self._param_strings(rule, param_name),
                )

    def _op_line_prefix(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        yield from self._line_match(
            rule_index,
            rule,
            value,
            budget,
            mode="prefix",
            literals=(self._param_str(rule, "literal"),),
        )

    def _op_line_suffix(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        yield from self._line_match(
            rule_index,
            rule,
            value,
            budget,
            mode="suffix",
            literals=(self._param_str(rule, "literal"),),
        )

    def _op_line_equals(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        yield from self._line_match(
            rule_index,
            rule,
            value,
            budget,
            mode="equals",
            literals=(self._param_str(rule, "literal"),),
        )

    def _op_balanced_delimiter(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        opening = self._param_str(rule, "open")
        closing = self._param_str(rule, "close")
        stack: list[tuple[int, int]] = []
        index = 0
        while index < len(value.text):
            budget.step()
            if value.text.startswith(opening, index):
                stack.append(self._offset_position(value.text, index))
                index += len(opening)
            elif value.text.startswith(closing, index):
                line, column = self._offset_position(value.text, index)
                if stack:
                    stack.pop()
                else:
                    yield _RawMatch(
                        rule_index, rule, line, column, closing, budget.steps
                    )
                index += len(closing)
            else:
                index += 1
        for line, column in stack:
            yield _RawMatch(rule_index, rule, line, column, opening, budget.steps)

    def _op_changed_line_only(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        yield from self._literal_matches(
            rule_index,
            rule,
            value.text,
            self._param_str(rule, "literal"),
            budget,
            line_filter=frozenset(value.changed_lines),
        )

    def _op_bounded_context_contains(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        lines = self._lines(value.text)
        anchor = self._param_str(rule, "anchor")
        literal = self._param_str(rule, "literal")
        maximum = self._param_int(rule, "max_lines")
        for anchor_index, anchor_line in enumerate(lines):
            budget.step()
            if anchor not in anchor_line:
                continue
            start = max(0, anchor_index - maximum)
            stop = min(len(lines), anchor_index + maximum + 1)
            for line_index in range(start, stop):
                budget.step()
                column = lines[line_index].find(literal)
                if column >= 0:
                    yield _RawMatch(
                        rule_index,
                        rule,
                        line_index + 1,
                        column + 1,
                        f"{anchor}\x1f{literal}",
                        budget.steps,
                    )

    def _op_identifier_equals(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        identifier = self._param_str(rule, "identifier")
        for token in self._tokenize(value.text, budget):
            budget.step()
            if token.kind == "identifier" and token.value == identifier:
                yield _RawMatch(
                    rule_index,
                    rule,
                    token.line,
                    token.column,
                    identifier,
                    budget.steps,
                )

    def _op_call_name_equals(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        name = self._param_str(rule, "name")
        tokens = self._tokenize(value.text, budget)
        for index, token in enumerate(tokens[:-1]):
            budget.step()
            if (
                token.kind == "identifier"
                and token.value == name
                and tokens[index + 1].value == "("
            ):
                yield _RawMatch(
                    rule_index,
                    rule,
                    token.line,
                    token.column,
                    name,
                    budget.steps,
                )

    def _op_argument_literal_equals(
        self,
        rule_index: int,
        rule: DeclarativeRule,
        value: AuthorizedToolInput,
        budget: _ExecutionBudget,
    ) -> Iterator[_RawMatch]:
        call = self._param_str(rule, "call")
        literal = self._param_str(rule, "literal")
        tokens = self._tokenize(value.text, budget)
        for call_index, token in enumerate(tokens[:-1]):
            budget.step()
            if token.value != call or tokens[call_index + 1].value != "(":
                continue
            depth = 0
            for argument in tokens[call_index + 1 :]:
                budget.step()
                if argument.value == "(":
                    depth += 1
                elif argument.value == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif (
                    depth > 0
                    and argument.kind == "string"
                    and argument.value == literal
                ):
                    yield _RawMatch(
                        rule_index,
                        rule,
                        argument.line,
                        argument.column,
                        f"{call}\x1f{literal}",
                        budget.steps,
                    )
                    break
