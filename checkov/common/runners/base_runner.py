from __future__ import annotations

import itertools
import logging
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import List, Any, TYPE_CHECKING, TypeVar, Generic, Dict, Optional

from checkov.common.graph.db_connectors.networkx.networkx_db_connector import NetworkxConnector
from checkov.common.graph.graph_builder import CustomAttributes
from checkov.common.util.data_structures_utils import pickle_deepcopy
from checkov.common.util.tqdm_utils import ProgressBar

from checkov.common.graph.checks_infra.base_check import BaseGraphCheck
from checkov.common.output.report import Report
from checkov.runner_filter import RunnerFilter
from checkov.common.graph.graph_manager import GraphManager  # noqa

if TYPE_CHECKING:
    from checkov.common.checks_infra.registry import Registry
    from checkov.common.graph.checks_infra.registry import BaseRegistry
    from checkov.common.typing import _CheckResult, LibraryGraphConnector, LibraryGraph

_Context = TypeVar("_Context", bound="dict[Any, Any]|None")
_Definitions = TypeVar("_Definitions", bound="dict[Any, Any]|None")
_GraphManager = TypeVar("_GraphManager", bound="GraphManager[Any, Any]|None")


def strtobool(val: str) -> int:
    """Convert a string representation of truth to true (1) or false (0).

    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', and '0'.  Raises ValueError if
    'val' is anything else.
    """
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return 1
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return 0
    else:
        raise ValueError("invalid boolean value %r for environment variable CKV_IGNORE_HIDDEN_DIRECTORIES" % (val,))


IGNORED_DIRECTORIES_ENV = os.getenv("CKV_IGNORED_DIRECTORIES", "node_modules,.terraform,.serverless")
IGNORE_HIDDEN_DIRECTORY_ENV = strtobool(os.getenv("CKV_IGNORE_HIDDEN_DIRECTORIES", "True"))

ignored_directories = IGNORED_DIRECTORIES_ENV.split(",")


class BaseRunner(ABC, Generic[_Definitions, _Context, _GraphManager]):
    check_type = ""
    definitions: _Definitions | None = None
    raw_definitions: dict[str, list[tuple[int, str]]] | None = None
    context: _Context | None = None
    breadcrumbs = None
    external_registries: list[BaseRegistry] | None = None
    graph_manager: _GraphManager | None = None
    graph_registry: Registry | None = None
    db_connector: LibraryGraphConnector
    resource_subgraph_map: Optional[dict[str, str]] = None

    def __init__(self, file_extensions: Iterable[str] | None = None, file_names: Iterable[str] | None = None):
        self.file_extensions = file_extensions or []
        self.file_names = file_names or []
        self.pbar = ProgressBar(self.check_type)
        db_connector_class: "type[NetworkxConnector | RustworkxConnector]" = NetworkxConnector
        graph_framework = os.getenv("CHECKOV_GRAPH_FRAMEWORK", "RUSTWORKX")
        if graph_framework == "RUSTWORKX":
            from checkov.common.graph.db_connectors.rustworkx.rustworkx_db_connector import RustworkxConnector
            db_connector_class = RustworkxConnector

        self.db_connector = db_connector_class()

    @abstractmethod
    def run(
            self,
            root_folder: str | None,
            external_checks_dir: list[str] | None = None,
            files: list[str] | None = None,
            runner_filter: RunnerFilter | None = None,
            collect_skip_comments: bool = True,
    ) -> Report | list[Report]:
        pass

    def should_scan_file(self, filename: str) -> bool:
        # runners that are always applicable can do nothing and be included
        if not self.file_extensions and not self.file_names:
            return True

        basename = os.path.basename(filename)
        if basename and self.file_names and basename in self.file_names:
            return True

        extension = os.path.splitext(filename)[1]
        if extension and self.file_extensions and extension in self.file_extensions:
            return True

        return False

    def included_paths(self) -> Iterable[str]:
        return []

    def set_external_data(
            self,
            definitions: _Definitions | None,
            context: _Context | None,
            breadcrumbs: dict[str, dict[str, Any]] | None,
            **kwargs: Any,
    ) -> None:
        self.definitions = definitions
        self.context = context
        self.breadcrumbs = breadcrumbs

    def set_raw_definitions(self, definitions_raw: dict[str, list[tuple[int, str]]] | None) -> None:
        self.definitions_raw = definitions_raw

    def populate_metadata_dict(self) -> None:
        return None

    def load_external_checks(self, external_checks_dir: List[str]) -> None:
        return None

    def get_graph_checks_report(self, root_folder: str, runner_filter: RunnerFilter) -> Report:
        return Report(check_type="not_defined")

    def run_graph_checks_results(self, runner_filter: RunnerFilter, report_type: str, graph: LibraryGraph | None = None
                                 ) -> dict[BaseGraphCheck, list[_CheckResult]]:
        checks_results: "dict[BaseGraphCheck, list[_CheckResult]]" = {}
        if graph is None and (not self.graph_manager or not self.graph_registry):
            # should not happen
            logging.warning("Graph components were not initialized")
            return checks_results

        if graph is None and isinstance(self.graph_manager, GraphManager):
            graph = self.graph_manager.get_reader_endpoint()
        for r in itertools.chain(self.external_registries or [], [self.graph_registry]):
            r.load_checks()  # type:ignore[union-attr]
            registry_results = r.run_checks(graph, runner_filter, report_type)  # type:ignore[union-attr]
            checks_results = {**checks_results, **registry_results}
        # Filtering the checks now
        filtered_result: Dict[BaseGraphCheck, List[_CheckResult]] = {}
        for check, results in checks_results.items():
            filtered_result[check] = [result for result in results if runner_filter.should_run_check(
                check,
                check_id=check.id,
                file_origin_paths=[result.get("entity", {}).get(CustomAttributes.FILE_PATH, "")],
                report_type=self.check_type
            )]

        self._update_check_correct_connected_node(filtered_result)

        return filtered_result

    @staticmethod
    def _extract_relevant_resource_types(check_connected_resource_types: list[tuple[str]],
                                         connected_nodes_per_resource_types: dict[tuple[str], Any]) ->\
            tuple[str] | None:
        return next((resource_types for resource_types in check_connected_resource_types
                     if resource_types in connected_nodes_per_resource_types), None)

    @staticmethod
    def _get_connected_resources_types_with_subchecks(check: BaseGraphCheck) -> list[tuple[str]]:
        resource_types_tuples: list[tuple[str]] = []
        for sub_check in check.sub_checks:
            resource_types_tuples.append(tuple(sub_check.connected_resources_types))  # type: ignore
            resource_types_tuples.extend(
                BaseRunner._get_connected_resources_types_with_subchecks(sub_check))  # Recursive call
        return resource_types_tuples

    @staticmethod
    def _update_check_correct_connected_node(filtered_result: dict[BaseGraphCheck, list[_CheckResult]]) -> None:
        """
        Responsible for choosing the correct connected node per check (if exists), as every graph check may refer to
        a different connection that a resource might have.
        Before: connected_node could be a dict[tuple[resource_types], attributes].
        After: connected_node == attributes (of relevant connected node)
        """
        for check, results in filtered_result.items():
            for result in results:
                result["entity"] = pickle_deepcopy(result["entity"])  # Important to avoid changes between checks
                connected_node = result.get("entity", {}).get(CustomAttributes.CONNECTED_NODE)
                if connected_node is None:
                    continue

                check_connected_resource_types = BaseRunner._get_connected_resources_types_with_subchecks(check)

                check_relevant_connected_resource_types = BaseRunner._extract_relevant_resource_types(
                    check_connected_resource_types, connected_node)

                if check_relevant_connected_resource_types and \
                        check_relevant_connected_resource_types in connected_node:
                    result["entity"][CustomAttributes.CONNECTED_NODE] = \
                        connected_node[check_relevant_connected_resource_types]
                else:
                    result["entity"][CustomAttributes.CONNECTED_NODE] = None


def filter_ignored_paths(
    root_dir: str,
    names: list[str] | list[os.DirEntry[str]],
    excluded_paths: list[str] | None,
    included_paths: Iterable[str] | None = None
) -> None:
    # we need to handle legacy logic, where directories to skip could be specified using the env var (default value above)
    # or a directory starting with '.'; these look only at directory basenames, not relative paths.
    #
    # But then any other excluded paths (specified via --skip-path or via the platform repo settings) should look at
    # the path name relative to the root folder. These can be files or directories.
    # Example: take the following dir tree:
    # .
    #   ./dir1
    #      ./dir1/dir33
    #      ./dir1/.terraform
    #   ./dir2
    #      ./dir2/dir33
    #      /.dir2/hello.yaml
    #
    # if excluded_paths = ['dir1/dir33', 'dir2/hello.yaml'], then we would scan dir1, but we would skip its subdirectories. We would scan
    # dir2 and its subdirectory, but we'd skip hello.yaml.

    # first handle the legacy logic - this will also remove files starting with '.' but that's probably fine
    # mostly this will just remove those problematic directories hardcoded above.
    included_paths = included_paths or []
    for entry in list(names):
        cur_path: str = str(entry.name) if isinstance(entry, os.DirEntry) else str(entry)
        if cur_path in ignored_directories:
            safe_remove(names, entry)
        if cur_path.startswith(".") and IGNORE_HIDDEN_DIRECTORY_ENV and cur_path not in included_paths:
            safe_remove(names, entry)

    # now apply the new logic
    # TODO this is not going to work well on Windows, because paths specified in the platform will use /, and
    #  paths specified via the CLI argument will presumably use \\
    if excluded_paths:
        compiled = []
        for p in excluded_paths:
            try:
                compiled.append(re.compile(re.escape(p) if re.match(r'^\.[^\.]', p) else p))
            except re.error:
                # do not add compiled paths that aren't regexes
                continue
        for entry in list(names):
            path: str = str(entry.name) if isinstance(entry, os.DirEntry) else str(entry)
            full_path = os.path.join(root_dir, path)
            if any(pattern.search(full_path) for pattern in compiled) or any(p in full_path for p in excluded_paths):
                safe_remove(names, entry)


def safe_remove(names: list[Any], path: Any) -> None:
    if path in names:
        names.remove(path)
