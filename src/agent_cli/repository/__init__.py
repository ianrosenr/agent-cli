"""Repository memory: a rebuildable fact index and low-cost task inspection.

This is derived state, separate from canonical task state (``agent_cli.state``). It makes no
model calls and needs no vector database. See docs/REPOSITORY_INSPECTION.md.

Consumers such as skill routing (V1-06) use ``TaskInspector`` and ``RepositoryFacts``; they
never scan repository contents themselves.
"""

from agent_cli.repository.analysis_cache import AnalysisCache
from agent_cli.repository.analyzers import Analysis, Analyzer, AnalyzerSet, Import, Symbol
from agent_cli.repository.git import GitWorkspace, RepositoryError
from agent_cli.repository.index import (
    AnalysisState,
    Fact,
    FactSource,
    FileRecord,
    IndexInfo,
    RefreshReport,
    Relation,
    RepositoryIndex,
    SymbolRecord,
)
from agent_cli.repository.inspection import (
    Anchors,
    InspectedFile,
    Inspection,
    Reason,
    RepositoryFacts,
    TaskInspector,
    Weight,
)
from agent_cli.repository.python_analyzer import PythonAnalyzer

__all__ = [
    "Analysis",
    "AnalysisCache",
    "AnalysisState",
    "Analyzer",
    "AnalyzerSet",
    "Anchors",
    "Fact",
    "FactSource",
    "FileRecord",
    "GitWorkspace",
    "Import",
    "IndexInfo",
    "InspectedFile",
    "Inspection",
    "PythonAnalyzer",
    "Reason",
    "RefreshReport",
    "Relation",
    "RepositoryError",
    "RepositoryFacts",
    "RepositoryIndex",
    "Symbol",
    "SymbolRecord",
    "TaskInspector",
    "Weight",
]
