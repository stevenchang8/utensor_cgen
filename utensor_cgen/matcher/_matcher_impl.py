import logging
from collections import deque
from copy import deepcopy
from itertools import product

import attr
from attr.validators import instance_of
from click import style

from utensor_cgen.ir import (MetaOperationInfo, OperationInfo, uTensorGraph,
                             uTensorGraphView)
from utensor_cgen.matcher._morphism import Morphism
from utensor_cgen.utils import ops_bfs_queue, prune_graph, random_str, topologic_order_graph

__all__ = ["uTensorGraphMatcher", "uTensorGraphMatch"]
logger = logging.getLogger(__name__)

@attr.s(frozen=True, slots=True)
class OpEqualityDelegate(object):

  # op_type -> list[tuple] (permutations)
  _association_map = {}
  # op_type -> dict[op_type] -> morphism
  _compatibility_map = {}

  @classmethod
  def is_associative(cls, permutations):
    def deco(op):
      if op.op_type in cls._association_map:
        raise ValueError(
          "duplicate associativity definition found for {}".format(op.op_type)
        )
      assert (
        isinstance(permutations, tuple) and 
        all([isinstance(perm, tuple) for perm in permutations])
      ), "`permutations` should be tuple of int tuples"
      cls._association_map[op.op_type] = permutations
      return op
    return deco

  @classmethod
  def is_compatible_with(cls, other_op_type, morphism_type, **kwargs):
    if not issubclass(morphism_type, Morphism):
      raise ValueError(
        'expecting Morphism for `morphism`, get {}'.format(morphism_type)
      )
    def deco(op):
      if op.op_type not in cls._compatibility_map:
        cls._compatibility_map[op.op_type] = {}
      if other_op_type in cls._compatibility_map[op.op_type]:
        raise RuntimeError(
          "Multiple morphisms from {} to {} detected".format(op.op_type, other_op_type)
        )
      if not other_op_type in cls._compatibility_map[op.op_type]:
        cls._compatibility_map[op.op_type][other_op_type] = morphism_type(**kwargs)
      return op
    return deco

  @classmethod
  def query(cls, sub_op, patrn_op):
    """
    Parameters
    ----------
    sub_op : OperationInfo
      the op in the subject ugraph
    patrn_op : OperationInfo
      the op in the pattern ugraph to match with

    Return
    ------
    is_eq : bool
      these two ops are equivalent if True, False o.w.
    equivalent_ops : List[OperationInfo]
      a list of equivalent ops derieved from `sub_op`
    """
    cls._setup()
    logger.info('matching {} --> {}', patrn_op, sub_op)
    is_eq = False
    equivalent_ops = []
    if sub_op is None or patrn_op is None:
      # null tensor, do nothing
      pass
    elif sub_op.op_type == patrn_op.op_type and sub_op.op_type not in cls._association_map:
      is_eq = True
      equivalent_ops = [sub_op]
    elif sub_op.op_type == patrn_op.op_type:
      is_eq = True
      equivalent_ops = []
      for perm in cls._association_map[sub_op.op_type]:
        equivalent_ops.append(
          OperationInfo(
            name=sub_op.name,
            lib_name=sub_op.lib_name,
            ugraph=None,
            input_tensor_names=[sub_op.input_tensor_names[j] for j in perm],
            n_inputs=sub_op.n_inputs,
            output_tensor_names=sub_op.output_tensor_names[:],
            n_outputs=sub_op.n_outputs,
            op_type=sub_op.op_type,
            op_attr=sub_op.op_attr,
          )
        )
    elif patrn_op.op_type in cls._compatibility_map.get(sub_op.op_type, []):
      is_eq = True
      morphism = cls._compatibility_map[sub_op.op_type][patrn_op.op_type]
      equivalent_ops = [MetaOperationInfo(op_info=sub_op, morphism=morphism)]

    return is_eq, equivalent_ops
  
  @classmethod
  def _setup(cls):
    # to activate all configurations
    import utensor_cgen.backend.utensor._operators

@attr.s
class uTensorGraphMatcher(object):
  """
  Isomorphic Subgraph Matcher

  Perform isomorphic subgraph match against given graph

  A minimal example

  .. code-block:: python

    # Example: match and replace
    patrn_ugraph = ... # load the pattern uTensorGraph
    
    # create a matcher
    matcher = uTensorGraphMatcher(pattern_ugraph=patrn_ugraph)

    # define a callback
    def callback(match):
      # inspect the match object
      ....
      # return the replacing graph, input and output map
      return repl_ugraph, input_map, output_map
    
    # load the subject uTensorGraph
    subj_ugraph = ...

    # search for 1 match
    matches = matcher.match(subj_ugraph, n=1) # return a list of uTensorGraphMatch objects

    if matches:
      match = matches[0]
      match.replace_with(callback)

  See also :py:class:`.uTensorGraphMatch`

  :param pattern_ugraph: a graph serve as pattern to look for
  :type pattern_ugraph: :py:class:`.uTensorGraph`
  """

  pattern_ugraph = attr.ib(validator=instance_of(uTensorGraph))

  def match(self, other_ugraph, n=1):
    """
    Match the pattern against the graph

    :param other_ugraph: the graph where to search the pattern
    :type other_ugraph: :py:class:`.uTensorGraph`
    
    :param n: the maximum matches to return
    :type n: int

    :rtype: List[:py:class:`.uTensorGraphMatch`]
    """
    match_gen = self._match(other_ugraph)
    matches = []
    try:
      for _ in range(n):
        matches.append(next(match_gen))
    except StopIteration:
      pass
    return matches

  def match_all(self, other_ugraph):
    return list(self._match(other_ugraph))

  def _match(self, other_ugraph):
    outputs_pool = []
    for op in self.pattern_ugraph.output_ops:
      same_ops = other_ugraph.get_ops_by_type(op.op_type)
      if not same_ops:
        # there are missing output(s)
        # no way to match, return empty list
        return []
      outputs_pool.append(same_ops)
    output_candidates = product(*outputs_pool)
    for outputs in output_candidates:
      new_ugraph = deepcopy(other_ugraph)
      states = [
        _MatchState(
          match=uTensorGraphMatch(
            pattern_ugraph=self.pattern_ugraph,
            subject_ugraph=new_ugraph
          ),
          sub_bfs_queue=ops_bfs_queue(new_ugraph, init_nodes=outputs),
          patrn_bfs_queue=ops_bfs_queue(self.pattern_ugraph),
        )
      ]
      while True:
        visited_states = self._visit(states)
        if not visited_states:
          break
        states = []
        for state in visited_states:
          if state.is_done:
            input_checked = True
            for patrn_op in self.pattern_ugraph.input_ops:
              subj_op = state.match.patrn2subj_op_map[patrn_op.name]
              if len(subj_op.input_tensors) != len(patrn_op.input_tensors):
                input_checked = False
                break
              for (patrn_tensor, subj_tensor) in zip(patrn_op.input_tensors, subj_op.input_tensors):
                state.match.subj2patrn_tensor_map[subj_tensor.name] = patrn_tensor
                state.match.patrn2subj_tensor_map[patrn_tensor.name] = subj_tensor
            if input_checked:
              # FIXME: the ownership of ops/tensors in the match's map is incorrect.
              # That is, the match.subject_ugraph does not own some of the ops/tensors.
              # For now, we make a call to _sanitize_match as a workaround
              yield self._sanitize_match(state.match)
          else:
            states.append(state)

  def _visit(self, states):
    # visit the state with a top-down bfs fashion
    # return the states that are still matched
    new_states = []
    for state in states:
      match = state.match
      sub_op = state.sub_bfs_queue.popleft()
      patrn_op = state.patrn_bfs_queue.popleft()
      is_eq, eq_ops = OpEqualityDelegate.query(sub_op, patrn_op)
      if is_eq:
        for eq_op in eq_ops:
          new_subj_graph = deepcopy(match.subject_ugraph)
          eq_op.move_into(new_subj_graph, force=True)
          new_sub_bfs_queue = deque([new_subj_graph.ops_map[op.name] for op in state.sub_bfs_queue])
          in_nodes = eq_op.input_nodes
          in_names = set([op.name for op in in_nodes])
          in_idx = 0
          for i, op in enumerate(new_sub_bfs_queue):
            if op.name in in_names:
              new_sub_bfs_queue[i] = in_nodes[in_idx]
              in_idx += 1
              if in_idx >= len(in_nodes):
                break
          new_state = _MatchState(
            match=uTensorGraphMatch(
              pattern_ugraph=match.pattern_ugraph,
              subject_ugraph=new_subj_graph,
              patrn2subj_op_map={k: new_subj_graph.ops_map[v.name] for k, v in match.patrn2subj_op_map.items()},
              subj2patrn_op_map={k: match.pattern_ugraph.ops_map[v.name] for k, v in match.subj2patrn_op_map.items()},
              patrn2subj_tensor_map={k: new_subj_graph.tensors_map[v.name] for k, v in match.patrn2subj_tensor_map.items()},
              subj2patrn_tensor_map={k: match.pattern_ugraph.tensors_map[v.name] for k, v in match.subj2patrn_tensor_map.items()}
            ),
            sub_bfs_queue=new_sub_bfs_queue,
            patrn_bfs_queue=deque(state.patrn_bfs_queue),
          )
          new_state = self._handle_null_tensors(new_state, eq_op, patrn_op)
          new_state.match.update_op_map(patrn_op, eq_op)
          new_states.append(new_state)
    return new_states
  
  def _handle_null_tensors(self, state, subj_op, patrn_op):
    subj_ugraph = state.match.subject_ugraph
    for idx, patrn_tensor in enumerate(patrn_op.input_tensors):
      if patrn_tensor.is_null_tensor:
        subj_tensor = subj_op.input_tensors[idx]
        subj_ops_to_remove = set([subj_tensor.op.name])
        all_subgraph_ops = set(op.name for op in ops_bfs_queue(subj_ugraph, [subj_tensor.op]))
        for op_name in all_subgraph_ops:
          if all(out_op.name in all_subgraph_ops for out_op in subj_ugraph[op_name].output_nodes):
            subj_ops_to_remove.add(op_name)
        state.sub_bfs_queue = deque(
          [op for op in state.sub_bfs_queue if op.name not in subj_ops_to_remove]
        )
    return state

  def _sanitize_match(self, match):
    for op in match.patrn2subj_op_map.values():
      op.move_into(match.subject_ugraph)
    for tensor in match.patrn2subj_tensor_map.values():
      tensor.move_into(match.subject_ugraph)
    return match

@attr.s(repr=False)
class uTensorGraphMatch(object):
  """
  A isomorphic subgraph match

  See also :py:class:`.uTensorGraphMatcher`
  
  :param pattern_ugraph: the parttern graph
  :type pattern_ugraph: :py:class:`.uTensorGraph`

  :param subject_ugraph: the subjective graph
  :type subject_ugraph: :py:class:`.uTensorGraph`

  :param patrn2subj_op_map: a dict with key as op name in the :py:attr:`pattern_ugraph`
    and value as the matched op in the :py:attr:`subject_ugraph`
  :type patrn2subj_op_map: dict

  :param subj2patrn_op_map: a dict with key as op name in the :py:attr:`subject_ugraph`
    and value as the matched op in the :py:attr:`pattern_ugraph`
  :type subj2patrn_op_map: dict

  :param patrn2subj_tensor_map: a dict with key as the tensor object in the :py:attr:`pattern_ugraph`
    and value as the tensor object in the :py:attr:`subject_ugraph`
  :type patrn2subj_tensor_map: dict

  :param subj2patrn_tensor_map: a dict with key as the tensor object in the :py:attr:`subject_ugraph`
    and value as the tensor object in the :py:attr:`pattern_ugraph`
  :type subj2patrn_tensor_map: dict
  """

  pattern_ugraph = attr.ib(type=uTensorGraph)
  subject_ugraph = attr.ib(type=uTensorGraph)

  # map from op_name to op_info
  patrn2subj_op_map = attr.ib(factory=dict)
  subj2patrn_op_map = attr.ib(factory=dict)
  # tensor in pattern -> tensor in target
  patrn2subj_tensor_map = attr.ib(factory=dict)
  # tensor in target -> tensor in pattern
  subj2patrn_tensor_map = attr.ib(factory=dict)

  def update_op_map(self, pattern_op, subj_op):
    self.patrn2subj_op_map[pattern_op.name] = subj_op
    self.subj2patrn_op_map[subj_op.name] = pattern_op
    for pattern_tensor, target_tensor in zip(pattern_op.output_tensors, subj_op.output_tensors):
      self.patrn2subj_tensor_map[pattern_tensor.name] = target_tensor
      self.subj2patrn_tensor_map[target_tensor.name] = pattern_tensor
  
  @property
  def _is_valid(self):
    """Check if the match is valid

    1. only input/output ops of the subgraph view are allowed to have external linkage
    2. input ops have only external linkage for its inputs
    3. output ops have only external linkage for its outputs

    If any of above fail, there is no trivial way to determine how to replace the matched
    subgraph other than very hard code way.
    """
    subj_view = self.subject_graph_view
    valid = True
    checked_ops = set()
    for in_op in subj_view.input_ops:
      for op in in_op.output_nodes:
        if op.name not in subj_view.ops_map:
          valid = False
      checked_ops.add(in_op.name)
    for out_op in subj_view.output_ops:
      for op in out_op.input_nodes:
        if op.name not in subj_view.ops_map:
          valid = False
      checked_ops.add(out_op.name)
    
    for name, op in subj_view.ops_map.items():
      if name in checked_ops:
        continue
      for in_op in op.input_nodes:
        if in_op.name not in subj_view.ops_map:
          valid = False
      for out_op in op.output_nodes:
        if out_op.name not in subj_view.ops_map:
          valid = False
    return valid
    
  def replace_with(self, callback, suffix=None):
    """
    Replace matched subgraph with a given ugraph given by the callback, **not** in-place

    :param callback: a callable object which takes a :py:class:`.uTensorGraphMatch` and
      reutrn three values -- a :py:class:`.uTensorGraph` object to replaced the matched
      subgraph with (the ``replacing graph``), an ``input_map`` (dict) maps input tensors 
      in pattern graph to the input tensors in replacing graph and an ``output_map`` (dict)
      which maps the output tensors
    :type callback: callable

    :param suffix: (optional) the suffix to add to the name of ops/tensors in the replacing
      graph returned by ``callback``. If not given, it will be a random string
    :type suffix: str

    :param ugraph: (optional) the ugraph where the replacement take place.
    :type ugraph: :py:class:`.uTensorGraphMatcher`

    :rtype: :py:class:`.uTensorGraph`, a **new** graph with matched subgraph replaced
    """
    # build a matched subgraph and pass it to callback
    # input/output_map (dict): 
    #  {
    #     tensor in pattern graph : tensor in replacing graph
    #  }
    new_ugraph = deepcopy(self.subject_ugraph)
    replace_ugraph, input_map, output_map = callback(self)
    replaceible, reasons = self._is_replacible(replace_ugraph, input_map, output_map)
    if not replaceible:
      raise ValueError(
        'matched subgraph can not be replaced with the ugraph given: {}'.format(reasons)
      )
    replace_ugraph, input_map, output_map = self.new_ugraph_with_suffix(
      replace_ugraph, input_map, output_map, suffix
    )
    replace_ugraph.unsafe_merge_into(new_ugraph)
    subj_graph_view = self.subject_graph_view
    # replacing output tensors
    for patrn_tensor_name, repl_tensor_name in output_map.items():
      subj_tensor = self.patrn2subj_tensor_map[patrn_tensor_name]
      for op in new_ugraph.ops_map.values():
        for i, tensor_name in enumerate(op.input_tensor_names):
          if tensor_name == subj_tensor.name:
            op.input_tensor_names[i] = repl_tensor_name
    # replacing input tensors
    for patrn_tensor_name, repl_tensor_name in input_map.items():
      subj_tensor = self.patrn2subj_tensor_map[patrn_tensor_name]
      repl_tensor = new_ugraph.tensors_map[repl_tensor_name]
      for op in repl_tensor.output_nodes:
        for i, tensor_name in enumerate(op.input_tensor_names):
          if tensor_name == repl_tensor.name:
            op.input_tensor_names[i] = subj_tensor.name
    topologic_order_graph(new_ugraph)
    new_ugraph = prune_graph(new_ugraph)
    return new_ugraph

  def _is_replacible(self, replace_ugraph, input_map, output_map):
    """Given a ugraph to replace with, check if it's replacible with 
    the matched sub ugraph
    """
    replacible = True
    reasons = []
    if not self._is_valid:
      replaceible = False
      reasons.append('the match is not valid')
    subj_graph_view = self.subject_graph_view
    if len(input_map) != len(subj_graph_view.input_tensors):
      replacible = False
      reasons.append('the number of input tensors does not match')
    if len(output_map) != len(subj_graph_view.output_tensors):
      replacible = False
      reasons.append('the number of output tensors does not match')
    for tensor_name in input_map:
      in_patrn_tensor = self.pattern_ugraph.tensors_map[tensor_name]
      in_tensor_names = set([t.name for t in self.pattern_ugraph.input_tensors])
      if not in_patrn_tensor.name in in_tensor_names:
        replacible = False
        reasons.append(
          '{} is not found in the pattern graph'.format(in_patrn_tensor.name)
        )
        continue
    for name in output_map:
      out_tensor_names = set([t.name for t in self.pattern_ugraph.output_tensors])
      out_patrn_tensor = self.pattern_ugraph.tensors_map[name]
      if not out_patrn_tensor.name in out_tensor_names:
        replacible = False
        reasons.append(
          '{} is not found in the pattern graph'.format(out_patrn_tensor.name)
        )
    return replacible, reasons

  @classmethod
  def _random_suffix(cls, length=8):
    return random_str(length)

  @classmethod
  def new_ugraph_with_suffix(cls, ugraph, input_map, output_map, suffix=None):
    if suffix is None:
      suffix = cls._random_suffix()
    
    new_ugraph = deepcopy(ugraph)
    new_ugraph.output_nodes = [
      '{}_{}'.format(name, suffix) for name in ugraph.output_nodes
    ]
    new_ugraph.topo_order = ['{}_{}'.format(name, suffix) for name in ugraph.topo_order]
    new_ops_map = {}
    new_tensors_map = {}
    for ori_op_name, op in new_ugraph.ops_map.items():
      new_op = deepcopy(op, {'ugraph': new_ugraph})
      new_op_name = '{}_{}'.format(ori_op_name, suffix)
      new_op.name = new_op_name
      for i, tensor_name in enumerate(new_op.input_tensor_names):
        tensor = ugraph.tensors_map[tensor_name]
        tensor_idx = tensor.name.split(':')[1]
        new_op.input_tensor_names[i] = '{}_{}:{}'.format(tensor.op_name, suffix, tensor_idx)
      for i, tensor_name in enumerate(new_op.output_tensor_names):
        tensor_idx = tensor_name.split(':')[1]
        new_op.output_tensor_names[i] = '{}:{}'.format(new_op_name, tensor_idx)
      new_ops_map[new_op_name] = new_op
    for tensor_name, tensor in new_ugraph.tensors_map.items():
      tensor_idx = tensor_name.split(':')[1]
      tensor.name = '{}_{}:{}'.format(tensor.op_name, suffix, tensor_idx)
      tensor.op_name = '{}_{}'.format(tensor.op_name, suffix)
      new_tensors_map[tensor.name] = tensor
    new_ugraph.ops_map = new_ops_map
    new_ugraph.tensors_map = new_tensors_map
    new_input_map = {}
    for patrn_tensor_name, subj_tensor_name in input_map.items():
      op_name, tensor_idx = subj_tensor_name.split(':')
      new_tensor_name = '{}_{}:{}'.format(op_name, suffix, tensor_idx)
      new_input_map[patrn_tensor_name] = new_tensor_name
    new_output_map = {}
    for patrn_tensor_name, subj_tensor_name in output_map.items():
      op_name, tensor_idx = subj_tensor_name.split(':')
      new_tensor_name = '{}_{}:{}'.format(op_name, suffix, tensor_idx)
      new_output_map[patrn_tensor_name] = new_tensor_name
    return new_ugraph, new_input_map, new_output_map

  @property
  def subject_graph_view(self):
    output_nodes = [
      self.patrn2subj_op_map[name].name
      for name in self.pattern_ugraph.output_nodes
    ]
    op_names = list(self.subj2patrn_op_map.keys())
    return uTensorGraphView(
      ugraph=self.subject_ugraph,
      op_names=op_names,
      output_nodes=output_nodes,
    )

  @property
  def is_dangling(self):
    for op in self.patrn2subj_op_map.values():
      if op.ugraph is not self.subject_ugraph:
        return True
    for op in self.subj2patrn_op_map.values():
      if op.ugraph is not self.pattern_ugraph:
        return True
    for tensor in self.patrn2subj_tensor_map.values():
      if tensor.ugraph is not self.subject_ugraph:
        return True
    for tensor in self.subj2patrn_tensor_map.values():
      if tensor.ugraph is not self.pattern_ugraph:
        return True
    return False
  
  def __repr__(self):
    repr_str = self.__class__.__name__ + '(\n'
    repr_str += style('    <op in pattern graph> -> <op in subject graph>', bold=True) + '\n'
    for patrn_op_name, subj_op in self.patrn2subj_op_map.items():
      repr_str += '    {} -> {}\n'.format(patrn_op_name, subj_op.name)
    repr_str += ')'
    return repr_str

@attr.s
class _MatchState(object):
  match = attr.ib()
  @match.validator
  def check(self, attrib, value):
    if not isinstance(value, uTensorGraphMatch):
      raise ValueError(
        'expecting a uTensorGraphMatch, get {}'.format(type(value))
      )
  # sub_bfs_queue is a queue for BFS of the subject ugraph
  sub_bfs_queue = attr.ib(validator=instance_of(deque))
  # consume_queue is a queue defines the matching order of pattern ugraph
  patrn_bfs_queue = attr.ib(validator=instance_of(deque))
  visited = attr.ib(init=False, factory=set)

  @property
  def is_done(self):
    """
    a state is done, if
    1. the patrn_bfs_queue is empty
    """
    return not (self.patrn_bfs_queue and self.sub_bfs_queue)
