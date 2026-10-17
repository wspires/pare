//! The Drain log-template miner: the `Drain` class in `proto/pare.py`,
//! itself the core of drain3 0.9.11 (MIT; github.com/logpai/Drain3).
//!
//! Each masked record head goes in; out comes the cluster (template) it
//! belongs to. A prefix tree keyed on the token count and the first token
//! leads to candidate clusters; the message joins the most similar one at
//! or above the similarity threshold, its differing tokens becoming `<*>`,
//! or starts a new cluster. Template ids (`T1`, `T2`, ...) depend on this
//! clustering exactly, and `pare --only T3 < runfile` relies on them, so
//! every decision here follows the Python code, ties included.

use std::collections::HashMap;

use crate::pyclass::{py_isdigit, py_split};

/// 0.4 merges lines that share only a timestamp and level prefix.
pub const SIM_THRESHOLD: f64 = 0.5;
/// Tree depth: root, token count, first token, clusters.
pub const DEPTH: usize = 4;
/// At most this many children per tree node before new tokens share `<*>`.
pub const MAX_CHILDREN: usize = 100;
/// The wildcard a differing token becomes.
pub const WILDCARD: &str = "<*>";

/// One template: its tokens (wildcards as `<*>`), its id, and its size.
#[derive(Debug, Clone)]
pub struct Cluster {
    /// The template's tokens.
    pub tokens: Vec<String>,
    /// Drain's id: 1, 2, 3, ... in order of creation.
    pub id: usize,
    /// Messages clustered here.
    pub size: usize,
}

impl Cluster {
    /// The template as text: its tokens joined by single spaces.
    #[must_use]
    pub fn template(&self) -> String {
        self.tokens.join(" ")
    }
}

// Rust note: a tree node owns its children directly, in the map; no
// pointers or reference counting are needed, because the tree is only
// ever walked from the root down. A recursive type is fine here since the
// recursion goes through `HashMap`, which allocates its entries on the
// heap (a struct containing itself by value would have infinite size).
#[derive(Debug, Default)]
struct Node {
    children: HashMap<String, Node>,
    cluster_ids: Vec<usize>,
}

/// The miner. Feed it messages with [`Drain::add`].
#[derive(Debug, Default)]
pub struct Drain {
    root: Node,
    // Rust note: clusters live in a `Vec` and are referred to by index
    // (id − 1) from the tree, rather than by reference. References into a
    // growing `Vec` would be invalidated when it reallocates, which the
    // borrow checker forbids outright; indices are the idiomatic way to
    // link things that refer to each other.
    clusters: Vec<Cluster>,
}

impl Drain {
    /// An empty miner.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The cluster with Drain id `id` (from 1).
    #[must_use]
    pub fn cluster(&self, id: usize) -> &Cluster {
        &self.clusters[id - 1]
    }

    /// Number of clusters so far.
    #[must_use]
    pub fn len(&self) -> usize {
        self.clusters.len()
    }

    /// Whether no message has been added.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.clusters.is_empty()
    }

    /// Clusters one message (split on whitespace) and returns its cluster's id.
    ///
    /// ```
    /// use pare::drain::Drain;
    /// let mut d = Drain::new();
    /// let a = d.add("<TS> ERROR worker <N> crashed");
    /// let b = d.add("<TS> ERROR worker <N> exited");
    /// assert_eq!(a, b);
    /// assert_eq!(d.cluster(a).template(), "<TS> ERROR worker <N> <*>");
    /// assert_eq!(d.add("one two three"), 2);
    /// ```
    pub fn add(&mut self, message: &str) -> usize {
        let tokens: Vec<&str> = py_split(message).collect();
        match self.tree_search(&tokens) {
            Some(id) => {
                let cluster = &mut self.clusters[id - 1];
                // Rust note: `iter_mut().zip(...)` walks the template and
                // the message side by side and changes the template in
                // place: one pass, no index arithmetic, no bounds checks.
                for (t, m) in cluster.tokens.iter_mut().zip(&tokens) {
                    if t != m && t != WILDCARD {
                        WILDCARD.clone_into(t);
                    }
                }
                cluster.size += 1;
                id
            }
            None => {
                let id = self.clusters.len() + 1;
                self.clusters.push(Cluster {
                    tokens: tokens.iter().map(|&t| t.to_owned()).collect(),
                    id,
                    size: 1,
                });
                self.add_to_tree(id);
                id
            }
        }
    }

    fn tree_search(&self, tokens: &[&str]) -> Option<usize> {
        let n = tokens.len();
        let mut node = self.root.children.get(&n.to_string())?;
        if n == 0 {
            return node.cluster_ids.first().copied();
        }
        let max_node_depth = DEPTH - 2;
        // Rust note: `(1..).zip(tokens)` pairs each token with a counter
        // that starts at 1, instead of a `depth` variable incremented by
        // hand at the bottom of the loop (which clippy flags): the counter
        // cannot be forgotten on a `continue` or updated twice.
        for (depth, &token) in (1..).zip(tokens) {
            if depth >= max_node_depth || depth == n {
                break;
            }
            node = node
                .children
                .get(token)
                .or_else(|| node.children.get(WILDCARD))?;
        }
        // The most similar candidate; ties go to more wildcards, then to
        // the earlier candidate. Wildcards do not count as matches.
        let mut best: Option<usize> = None;
        let (mut best_sim, mut best_params) = (-1.0_f64, -1_i64);
        for &id in &node.cluster_ids {
            let c = &self.clusters[id - 1];
            let (mut same, mut params) = (0_usize, 0_i64);
            for (t, m) in c.tokens.iter().zip(tokens) {
                if t == WILDCARD {
                    params += 1;
                } else if t == m {
                    same += 1;
                }
            }
            #[allow(clippy::cast_precision_loss)] // token counts are small
            let sim = same as f64 / c.tokens.len() as f64;
            #[allow(clippy::float_cmp)] // exact ties, as in Python
            let better = sim > best_sim || (sim == best_sim && params > best_params);
            if better {
                best = Some(id);
                best_sim = sim;
                best_params = params;
            }
        }
        best.filter(|_| best_sim >= SIM_THRESHOLD)
    }

    fn add_to_tree(&mut self, id: usize) {
        // Rust note: two fields of `self` are borrowed at once below: the
        // tree mutably and the cluster's tokens immutably. That is allowed
        // because they are different fields; a `&mut self` method call in
        // the loop would not be.
        let tokens = &self.clusters[id - 1].tokens;
        let n = tokens.len();
        let mut node = self.root.children.entry(n.to_string()).or_default();
        if n == 0 {
            node.cluster_ids = vec![id];
            return;
        }
        let max_node_depth = DEPTH - 2;
        for (depth, token) in (1..).zip(tokens) {
            if depth >= max_node_depth || depth >= n {
                node.cluster_ids.push(id);
                break;
            }
            let children = &mut node.children;
            let key = if children.contains_key(token.as_str()) {
                token.as_str()
            } else if token.chars().any(py_isdigit) {
                WILDCARD // a token with a digit is a parameter
            } else if children.contains_key(WILDCARD) {
                if children.len() < MAX_CHILDREN {
                    token.as_str()
                } else {
                    WILDCARD
                }
            } else if children.len() + 1 < MAX_CHILDREN {
                token.as_str()
            } else {
                WILDCARD // the last free slot becomes the wildcard
            };
            node = children.entry(key.to_owned()).or_default();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_and_separates_like_python() {
        // The same cases as test_drain_merges_and_parametrizes in test_pare.py.
        let mut d = Drain::new();
        let a = d.add("<TS> ERROR worker <N> crashed");
        let b = d.add("<TS> ERROR worker <N> exited");
        let c = d.add("one two three");
        assert_eq!((a, b, c), (1, 1, 2));
        assert_eq!(d.cluster(1).size, 2);
        assert_eq!(d.cluster(1).template(), "<TS> ERROR worker <N> <*>");
        assert_eq!(d.add("<TS> INFO started up"), 3); // under the similarity threshold
    }

    #[test]
    fn past_the_child_limit_tokens_share_the_wildcard() {
        // Numbers from proto/pare.py's Drain on the same messages: 99 first
        // tokens get their own node, the 100th slot becomes `<*>`, and every
        // later message (a token with a digit too) joins cluster 100.
        let mut d = Drain::new();
        let ids: Vec<usize> = (0..150u8)
            .map(|i| {
                let w = format!(
                    "w{}{}",
                    char::from(b'a' + i % 26),
                    char::from(b'a' + i / 26)
                );
                d.add(&format!("{w} x y"))
            })
            .collect();
        assert_eq!(&ids[98..103], [99, 100, 100, 100, 100]);
        assert_eq!(d.add("q1 x y"), 100);
        assert_eq!(d.len(), 100);
        assert_eq!(d.cluster(100).template(), "<*> x y");
    }
}
