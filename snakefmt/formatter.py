import re
import textwrap
from ast import parse as ast_parse
from copy import copy
from typing import Optional

import black

from snakefmt.config import PathLike, read_black_config
from snakefmt.exceptions import InvalidParameterSyntax, InvalidPython
from snakefmt.logging import Warnings
from snakefmt.parser.grammar import SnakeRule
from snakefmt.parser.parser import Parser, comment_start
from snakefmt.parser.syntax import (
    COMMENT_SPACING,
    TAB,
    InlineSingleParam,
    Parameter,
    ParameterSyntax,
    SingleParam,
    Syntax,
)
from snakefmt.types import TokenIterator

triple_quote_matcher = re.compile(
    r"^\s*(\w?\"{3}.*?\"{3})|^\s*(\w?'{3}.*?'{3})", re.DOTALL | re.MULTILINE
)
contextual_matcher = re.compile(
    r"(.*)^(if|elif|else|with|for|while)([^:]*)(:.*)", re.S | re.M
)


class Formatter(Parser):
    def __init__(
        self,
        snakefile: TokenIterator,
        line_length: Optional[int] = None,
        black_config_file: Optional[PathLike] = None,
    ):
        self.result: str = ""
        self.lagging_comments: str = ""
        self.no_formatting_yet: bool = True

        self.black_mode = read_black_config(black_config_file)

        if line_length is not None:
            self.black_mode.line_length = line_length

        super().__init__(snakefile)  # Call to parse snakefile

    def get_formatted(self) -> str:
        return self.result

    def flush_buffer(
        self,
        from_python: bool = False,
        final_flush: bool = False,
        in_global_context: bool = False,
    ) -> None:
        if len(self.buffer) == 0 or self.buffer.isspace():
            self.result += self.buffer
            self.buffer = ""
            return

        if not from_python:
            formatted = self.run_black_format_str(self.buffer, self.target_indent)
            if self.target_indent > 0:
                formatted = self.align_strings(formatted, self.target_indent)
        else:
            # Invalid python syntax, eg lone 'else:' between two rules, can occur.
            # Below constructs valid code statements and formats them.
            re_match = contextual_matcher.match(self.buffer)
            if re_match is not None:
                callback_keyword = re_match.group(2)
                used_keyword = (
                    "if" if callback_keyword in {"elif", "else"} else callback_keyword
                )
                condition = re_match.group(3)
                if condition != "":
                    test_substitute = f"{used_keyword}{condition}"
                else:
                    test_substitute = f"{used_keyword} a"
                to_format = (
                    f"{re_match.group(1)}{test_substitute}" f"{re_match.group(4)}pass"
                )
                formatted = self.run_black_format_str(to_format, self.target_indent)
                re_rematch = contextual_matcher.match(formatted)
                if condition != "":
                    callback_keyword += re_rematch.group(3)
                formatted = (
                    f"{re_rematch.group(1)}{callback_keyword}" f"{re_rematch.group(4)}"
                )
                formatted_lines = formatted.splitlines(keepends=True)
                formatted = "".join(formatted_lines[:-1])  # Remove the 'pass' line
            else:
                formatted = self.run_black_format_str(self.buffer, self.target_indent)
            code_indent = self.syntax.code_indent
            if code_indent is not None:
                formatted = textwrap.indent(formatted, f"{TAB * code_indent}")

        # Re-add newline removed by black for proper parsing of comments
        if self.buffer.endswith("\n\n"):
            if comment_start(self.buffer.rstrip().splitlines()[-1]):
                formatted += "\n"
        # Only stick together separated single-parm keywords when separated by comments
        buffer_is_all_comments = all(map(comment_start, self.buffer.splitlines()))
        if not buffer_is_all_comments:
            self.last_recognised_keyword = ""
        self.add_newlines(self.target_indent, formatted, final_flush, in_global_context)
        self.buffer = ""

    def process_keyword_context(self, in_global_context: bool):
        cur_indent = self.syntax.cur_indent
        self.add_newlines(cur_indent, in_global_context=in_global_context)
        formatted = f"{TAB * cur_indent}{self.syntax.keyword_line}"
        if self.syntax.enter_context:
            formatted += ":"
        formatted += f"{self.syntax.comment}\n"
        self.result += formatted
        self.last_recognised_keyword = self.syntax.keyword_name

    def process_keyword_param(
        self, param_context: ParameterSyntax, in_global_context: bool
    ):
        self.add_newlines(
            param_context.target_indent - 1,
            in_global_context=in_global_context,
            context=param_context,
        )
        in_rule = issubclass(param_context.incident_vocab.__class__, SnakeRule)
        self.result += self.format_params(param_context, in_rule)
        self.last_recognised_keyword = param_context.keyword_name

    def run_black_format_str(self, string: str, target_indent: int) -> str:
        # reduce black target line length according to how indented the code is
        current_line_length = target_indent * len(TAB)
        black_mode = copy(self.black_mode)
        black_mode.line_length = max(0, black_mode.line_length - current_line_length)
        try:
            fmted = black.format_str(string, mode=black_mode)
        except black.InvalidInput as e:
            raise InvalidPython(
                f"Got error:\n```\n{str(e)}\n```\n" f"while formatting code with black."
            ) from None
        return fmted

    def align_strings(self, string: str, target_indent: int) -> str:
        """
        Takes an ensemble of strings and indents/reindents it
        """
        pos = 0
        used_indent = TAB * target_indent
        indented = ""
        for match in re.finditer(triple_quote_matcher, string):
            indented += textwrap.indent(string[pos : match.start(1)], used_indent)
            match_slice = string[match.start(1) : match.end(1)].replace("\t", TAB)
            all_lines = match_slice.splitlines(keepends=True)
            first = textwrap.indent(textwrap.dedent(all_lines[0]), used_indent)
            indented += first
            if len(all_lines) > 2:
                middle = textwrap.indent(
                    textwrap.dedent("".join(all_lines[1:-1])), used_indent
                )
                indented += middle
            if len(all_lines) > 1:
                last = textwrap.indent(textwrap.dedent(all_lines[-1]), used_indent)
                indented += last
            pos = match.end()
        indented += textwrap.indent(string[pos:], used_indent)

        return indented

    def format_param(
        self,
        parameter: Parameter,
        target_indent: int,
        inline_formatting: bool,
        single_param: bool = False,
    ) -> str:
        string_indent = TAB * target_indent
        if inline_formatting:
            target_indent = 0
        val = str(parameter)

        try:
            # A snakemake parameter is syntactically like a function parameter
            ast_parse(f"param({val})")
        except SyntaxError:
            raise InvalidParameterSyntax(f"{parameter.line_nb}{val}") from None

        if inline_formatting:
            val = val.replace("\n", "")  # collapse strings on multiple lines
        try:
            val = self.run_black_format_str(val, target_indent)
        except InvalidPython:
            if "**" in val:
                val = val.replace("** ", "**")

        val = self.align_strings(val, target_indent)
        if parameter.has_a_key():  # Remove space either side of '='
            match_equal = re.match("(.*?) = (.*)", val, re.DOTALL)
            val = f"{match_equal.group(1)}={match_equal.group(2)}"

        result = ""
        if not inline_formatting:
            for comment in parameter.pre_comments:
                result += f"{string_indent}{comment}\n"
        result += val.strip("\n")
        if not single_param:
            result += ","
        post_comment_iter = iter(parameter.post_comments)
        if parameter._has_inline_comment:
            result += f"{COMMENT_SPACING}{next(post_comment_iter)}"
        result += "\n"
        for comment in post_comment_iter:
            result += f"{string_indent}{comment}\n"
        return result

    def format_params(self, parameters: ParameterSyntax, in_rule: bool) -> str:
        target_indent = parameters.target_indent
        used_indent = TAB * (target_indent - 1)

        p_class = parameters.__class__
        single_param = issubclass(p_class, SingleParam)
        inline_fmting = False
        if p_class is InlineSingleParam:
            inline_fmting = True

        result = f"{used_indent}{parameters.keyword_name}:"
        if inline_fmting:
            result += " "
            prepended_comments = ""
            if parameters.comment != "":
                prepended_comments += f"{used_indent}{parameters.comment.lstrip()}\n"
            param = next(iter(parameters.all_params))
            for comment in param.pre_comments:
                prepended_comments += f"{used_indent}{comment}\n"
            if prepended_comments != "":
                Warnings.comment_relocation(parameters.keyword_name, param.line_nb)
            result = f"{prepended_comments}{result}"
        else:
            result += f"{parameters.comment}\n"
        for param in parameters.all_params:
            result += self.format_param(
                param, target_indent, inline_fmting, single_param
            )
        num_c = len(param.post_comments)
        if num_c > 1 or (not param._has_inline_comment and num_c == 1):
            Warnings.block_comment_below(parameters.keyword_name, param.line_nb)
        return result

    def add_newlines(
        self,
        cur_indent: int,
        formatted_string: str = "",
        final_flush: bool = False,
        in_global_context: bool = False,
        context: Syntax = None,
    ):
        """
        Top-level (indent of 0) rules and python code get two newlines separation
        Indented rules/pycode get one newline separation
        Comments immediately preceding rules/pycode get newlined with them
        """
        comment_matches = 0
        comment_break = 1
        all_lines = formatted_string.splitlines()
        if len(all_lines) > 0:
            for line in reversed(all_lines):
                if not comment_start(line):
                    break
                comment_matches += 1
            comment_break = len(all_lines) - comment_matches

        have_only_comment_lines = comment_break == 0
        if not have_only_comment_lines or final_flush:
            collate_same_singleparamkeyword = (
                context is not None
                and context.keyword_name == self.last_recognised_keyword
                and issubclass(context.__class__, SingleParam)
            )
            if not self.no_formatting_yet and not collate_same_singleparamkeyword:
                if cur_indent == 0:
                    self.result += "\n\n"
                elif in_global_context:
                    self.result += "\n"
        if in_global_context:  # Deal with comments
            if self.lagging_comments != "":
                self.result += self.lagging_comments
                self.lagging_comments = ""

            if len(all_lines) > 0:
                if not have_only_comment_lines:
                    self.result += "\n".join(all_lines[:comment_break]).rstrip() + "\n"
                if comment_matches > 0:
                    self.lagging_comments = "\n".join(all_lines[comment_break:]) + "\n"
                    if final_flush:
                        self.result += self.lagging_comments
        else:
            self.result += formatted_string

        if self.no_formatting_yet:
            if comment_break > 0:
                self.no_formatting_yet = False
