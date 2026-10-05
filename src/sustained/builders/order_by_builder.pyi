from typing import Optional, Type

from ..compilers import Compiler
from ..model import Model
from ..types import ColumnReference

def reject_literal(column: object, method: str) -> None: ...

class OrderByClauseBuilder:
    def __init__(
        self, model_class: Type[Model], compiler: Optional[Compiler] = None
    ) -> None: ...
    def orderBy(
        self,
        column: ColumnReference,
        direction: str = "ASC",
        nulls: Optional[str] = None,
    ) -> None: ...
