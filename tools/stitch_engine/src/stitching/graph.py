"""Global stitch graph (CLAUDE.local.md #10).

Homographies are composed along a BFS shortest-path tree from a chosen
reference image, not accumulated as a naive linear chain (#10: "단순 chain
누적 ... 만 사용하지 않는다"). Images in a different connected component than
the reference are reported as unreachable rather than force-merged.
"""

from __future__ import annotations

import networkx as nx
import numpy as np

from src.common.types import GeometryResult


def build_stitch_graph(geometry_results: list[GeometryResult]) -> nx.Graph:
    g = nx.Graph()
    for r in geometry_results:
        if r.status != "OK":
            continue
        weight = 1.0 / max(r.inlier_ratio, 1e-6)
        g.add_edge(r.image_a, r.image_b, geom=r, weight=weight)
    return g


def pick_reference(g: nx.Graph) -> str | None:
    """Pick the best-connected node as the stitch reference frame (deterministic tie-break)."""
    if g.number_of_nodes() == 0:
        return None
    degrees = dict(g.degree())
    return max(degrees.items(), key=lambda kv: (kv[1], kv[0]))[0]


def compute_global_homographies(g: nx.Graph, reference: str) -> tuple[dict[str, np.ndarray], list[str]]:
    """Compose per-edge homographies along the BFS tree from `reference`.

    Returns (homographies mapping each reachable node -> reference frame,
    list of node ids in other connected components).
    """
    homographies: dict[str, np.ndarray] = {reference: np.eye(3)}
    for u, v in nx.bfs_edges(g, reference):
        geom = g.edges[u, v]["geom"]
        if geom.image_a == u and geom.image_b == v:
            h_v_to_u = np.linalg.inv(geom.homography)
        elif geom.image_a == v and geom.image_b == u:
            h_v_to_u = geom.homography
        else:
            raise AssertionError(f"edge/geom id mismatch for ({u}, {v})")
        homographies[v] = homographies[u] @ h_v_to_u

    unreachable = [n for n in g.nodes if n not in homographies]
    return homographies, unreachable


def count_connected_components(g: nx.Graph) -> int:
    if g.number_of_nodes() == 0:
        return 0
    return nx.number_connected_components(g)


def _apply_homography(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    homog = np.hstack([pts, np.ones((pts.shape[0], 1))])
    proj = homog @ H.T
    return proj[:, :2] / proj[:, 2:3]


def compute_drift_score(
    g: nx.Graph,
    reference: str,
    homographies: dict[str, np.ndarray],
    sizes: dict[str, tuple[int, int]],
) -> tuple[float | None, float | None, int]:
    """Cycle-consistency drift (CLAUDE.local.md #10/#11/#12).

    For every graph edge that is NOT part of the BFS spanning tree used to
    compose `homographies`, compare the directly measured pair homography
    against the one predicted by chaining through the reference frame.
    Disagreement here is accumulated chain error a naive tree traversal
    can't self-correct — the signal #12 uses to decide a COLMAP fallback is
    warranted.

    Returns (mean_drift_px, max_drift_px, cycle_edge_count). The first two
    are None when the graph has no cycle-closing edge to check against
    (nothing to disagree with, not evidence of zero drift).
    """
    tree_edges = {frozenset(e) for e in nx.bfs_edges(g, reference)}
    cycle_edges = [e for e in g.edges if frozenset(e) not in tree_edges]

    errors: list[float] = []
    for u, v in cycle_edges:
        if u not in homographies or v not in homographies or u not in sizes:
            continue
        geom = g.edges[u, v]["geom"]
        if geom.image_a == u and geom.image_b == v:
            h_u_to_v_direct = geom.homography
        elif geom.image_a == v and geom.image_b == u:
            h_u_to_v_direct = np.linalg.inv(geom.homography)
        else:
            continue
        h_u_to_v_predicted = np.linalg.inv(homographies[v]) @ homographies[u]

        w, h = sizes[u]
        corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
        predicted = _apply_homography(h_u_to_v_predicted, corners)
        direct = _apply_homography(h_u_to_v_direct, corners)
        errors.extend(np.linalg.norm(predicted - direct, axis=1).tolist())

    if not errors:
        return None, None, len(cycle_edges)
    return float(np.mean(errors)), float(np.max(errors)), len(cycle_edges)
