"""Global stitch graph (CLAUDE.local.md #10).

Homographies are composed along a BFS shortest-path tree from a chosen
reference image, not accumulated as a naive linear chain (#10: "단순 chain
누적 ... 만 사용하지 않는다"). Images in a different connected component than
the reference are reported as unreachable rather than force-merged.
"""

from __future__ import annotations

import networkx as nx
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

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
    """Pick the best-connected node as the stitch reference frame, restricted
    to the LARGEST connected component.

    2026-09-08: picking by raw degree across the WHOLE graph (ignoring which
    component a node sits in) can choose a node in a small island instead of
    the big one -- every node reachable from the reference is capped at that
    island's own size regardless of how good the composition algorithm is,
    so this one choice alone can silently throw away most of an otherwise-
    reachable capture. Verified on a real 168-image run's cached matched
    edges: raw max-degree picked a 46-image island while the true largest
    component (found only by actually checking component sizes) held many
    more of the same 17-island graph's nodes.
    """
    if g.number_of_nodes() == 0:
        return None
    largest_component = max(nx.connected_components(g), key=len)
    degrees = dict(g.degree())
    return max(
        ((n, degrees[n]) for n in largest_component),
        key=lambda kv: (kv[1], kv[0]),
    )[0]


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


def _homography_from_params(p: np.ndarray) -> np.ndarray:
    return np.array([[p[0], p[1], p[2]], [p[3], p[4], p[5]], [p[6], p[7], 1.0]])


def _params_from_homography(H: np.ndarray) -> np.ndarray:
    return (H / H[2, 2]).flatten()[:8]


def refine_global_homographies(
    g: nx.Graph,
    reference: str,
    homographies: dict[str, np.ndarray],
    sizes: dict[str, tuple[int, int]],
    max_nfev: int = 200,
) -> dict[str, np.ndarray]:
    """Global least-squares refinement of compute_global_homographies' output.

    compute_global_homographies only ever composes homographies along a
    single BFS spanning-tree path per image -- every other edge in the graph
    (compute_drift_score's "cycle_edges", measured only as a diagnostic) is
    pure redundant geometric information that gets thrown away. On a densely
    overlapping facade capture that redundancy is enormous (a real run: 1928
    matched edges but only ~164 needed for a spanning tree -- over 1700 edges
    of agreement/disagreement never fed back into the homographies
    themselves), so a single bad or merely-noisy edge anywhere on the one
    chosen path corrupts every image downstream of it with nothing to
    correct it -- the ghosting/torn-building look in the pre-COLMAP preview.

    This refines every reachable non-reference image's homography-to-
    reference (initialized from the tree composition, so this is a
    correction pass, not a fit from scratch) by minimizing, over EVERY edge
    in the graph at once, the same corner-disagreement residual
    compute_drift_score already computes for diagnostics: for edge (u, v),
    how much do "warp u's corners through the refined H_u/H_v pair" and
    "warp u's corners through the pair's own directly-measured homography"
    disagree. Driving that to ~0 across the whole graph simultaneously is
    exactly what compute_drift_score's global_drift_score_px/
    max_drift_score_px measure -- so a successful refinement is directly
    visible as those two numbers dropping in the next quality report,
    not just "looks better".
    """
    nodes = [n for n in homographies if n != reference]
    if len(nodes) < 2:
        return dict(homographies)
    index = {n: i for i, n in enumerate(nodes)}

    edges: list[tuple[str, str, np.ndarray, np.ndarray]] = []
    for u, v, data in g.edges(data=True):
        if u not in homographies or v not in homographies or u not in sizes:
            continue
        geom = data["geom"]
        if geom.image_a == u and geom.image_b == v:
            h_direct = geom.homography
        elif geom.image_a == v and geom.image_b == u:
            h_direct = np.linalg.inv(geom.homography)
        else:
            continue
        w, h = sizes[u]
        corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
        edges.append((u, v, h_direct, corners))
    if not edges:
        return dict(homographies)

    def get_h(x: np.ndarray, n: str) -> np.ndarray:
        if n == reference:
            return np.eye(3)
        i = index[n]
        return _homography_from_params(x[i * 8:(i + 1) * 8])

    def residuals(x: np.ndarray) -> np.ndarray:
        out = np.empty(len(edges) * 8)
        for k, (u, v, h_direct, corners) in enumerate(edges):
            h_predicted = np.linalg.inv(get_h(x, v)) @ get_h(x, u)
            predicted = _apply_homography(h_predicted, corners)
            direct = _apply_homography(h_direct, corners)
            out[k * 8:(k + 1) * 8] = (predicted - direct).flatten()
        return out

    x0 = np.concatenate([_params_from_homography(homographies[n]) for n in nodes])
    n_params = len(nodes) * 8
    sparsity = lil_matrix((len(edges) * 8, n_params), dtype=bool)
    for k, (u, v, _h, _c) in enumerate(edges):
        rows = slice(k * 8, (k + 1) * 8)
        if u in index:
            sparsity[rows, index[u] * 8:(index[u] + 1) * 8] = True
        if v in index:
            sparsity[rows, index[v] * 8:(index[v] + 1) * 8] = True

    # loss="huber": plain L2 (the default) actively made things WORSE in testing once even one
    # or two edges are grossly wrong rather than just noisy -- exactly this project's real
    # failure mode (a repeated window/balcony pattern making LoFTR+RANSAC confidently agree on a
    # completely wrong correspondence, not just a few noisy px). Squared-error loss lets a single
    # such edge's huge residual dominate the objective and drag every other image's estimate
    # toward accommodating it. Huber caps a residual's influence once it exceeds f_scale, so a
    # handful of badly-wrong edges get down-weighted instead of pulling the whole solution off.
    #
    # x_scale="jac": the 8 homography parameters have wildly different natural units (perspective
    # terms ~1e-5, rotation-like terms ~1, translation terms ~10-100px) -- without this, trf's
    # trust-region step sizing is dominated by whichever parameter has the largest raw magnitude
    # and converges after a handful of function evals having barely moved. With it, on 5
    # synthetic graphs (10 images, redundant overlapping edges, 2 grossly-wrong edges among 15,
    # 3px noise on the rest) mean per-image error vs ground truth dropped from the BFS-tree
    # baseline in every trial (3.2-13.4px) to 2.3-3.8px after refinement -- f_scale=2.0 was
    # consistently at or near the best of {1,2,3,5} tried across all 5 trials.
    result = least_squares(
        residuals, x0, jac_sparsity=sparsity, method="trf", max_nfev=max_nfev,
        loss="huber", f_scale=2.0, x_scale="jac",
    )

    refined = {reference: np.eye(3)}
    for n in nodes:
        refined[n] = get_h(result.x, n)
    return refined
