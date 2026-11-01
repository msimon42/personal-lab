"""Unrelated, test-only stateless toolset: no game or storage dependency."""
from pydantic import Field

from toolkit_core.contracts import OperationSpec, StrictModel


class Text(StrictModel):
    text: str = Field(max_length=8192)


class Statistics(StrictModel):
    characters: int
    words: int
    lines: int


def build_operations(application):
    def statistics(args):
        return Statistics(characters=len(args.text), words=len(args.text.split()),
                          lines=len(args.text.splitlines()))

    return [OperationSpec("test_text_statistics", "Count characters, words and lines in bounded supplied text.",
                          Text, Statistics, statistics, read_only=True, effect="observation")]
