"""Cycle detection must handle graphs within the supported source-file limit."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from locus_core.graph import find_cycles_in_graph


class LargeDependencyGraphTests(unittest.TestCase):
    def test_long_acyclic_chain_does_not_overflow_python_stack(self):
        nodes = [f"module{i:04}.py" for i in range(2500)]
        graph = {node: {nodes[i + 1]} if i + 1 < len(nodes) else set()
                 for i, node in enumerate(nodes)}
        self.assertEqual(find_cycles_in_graph(graph), [])

    def test_long_cycle_returns_a_real_closed_path(self):
        nodes = [f"module{i:04}.py" for i in range(2500)]
        graph = {node: {nodes[(i + 1) % len(nodes)]} for i, node in enumerate(nodes)}
        cycles = find_cycles_in_graph(graph)
        self.assertEqual(len(cycles), 1)
        path = cycles[0]
        self.assertEqual(path[0], path[-1])
        self.assertEqual(set(path[:-1]), set(nodes))
        for source, target in zip(path, path[1:]):
            self.assertIn(target, graph[source])

    def test_linked_components_remain_distinct(self):
        graph = {"a": {"b"}, "b": {"a", "c"}, "c": {"d"}, "d": {"c"},
                 "entry": {"a"}, "leaf": set()}
        cycles = find_cycles_in_graph(graph)
        self.assertEqual({frozenset(path[:-1]) for path in cycles},
                         {frozenset({"a", "b"}), frozenset({"c", "d"})})

    def test_self_edge_does_not_hide_a_nontrivial_cycle(self):
        self.assertEqual(find_cycles_in_graph({"a": {"a", "b"}, "b": {"a"}}),
                         [["a", "b", "a"]])


if __name__ == "__main__":
    unittest.main()
