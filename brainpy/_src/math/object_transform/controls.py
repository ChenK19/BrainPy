# -*- coding: utf-8 -*-


from typing import Union, Sequence, Any, Dict, Callable, Optional

import jax
import jax.numpy as jnp
from jax import lax
from jax.errors import UnexpectedTracerError
from jax.tree_util import tree_flatten, tree_unflatten
from tqdm.auto import tqdm
from jax.experimental.host_callback import id_tap

from brainpy import errors, tools
from brainpy._src.math.interoperability import as_jax
from brainpy._src.math.ndarray import (Array, )
from ._tools import (evaluate_dyn_vars,
                     dynvar_deprecation,
                     node_deprecation,
                     abstract)
from .base import BrainPyObject, ObjectTransform
from .naming import (get_unique_name,
                     get_stack_cache,
                     cache_stack)
from .variables import (Variable, VariableStack)

__all__ = [
  'make_loop',
  'make_while',
  'make_cond',

  'cond',
  'ifelse',
  'for_loop',
  'while_loop',
]


class ControlObject(ObjectTransform):
  """Object-oriented Control Flow Transformation in BrainPy.
  """

  def __init__(
      self,
      call: Callable,
      dyn_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]],
      repr_fun: Dict,
      name=None
  ):
    super().__init__(name=name)

    self.register_implicit_vars(dyn_vars)
    self._f = call
    self._dyn_vars = dyn_vars
    self._repr_fun = repr_fun

  def __call__(self, *args, **kwargs):
    return self._f(*args, **kwargs)

  def __repr__(self):
    name = self.__class__.__name__
    format_ref = [f'{k}={tools.repr_context(tools.repr_object(v), " " * (len(name) + len(k)))}'
                  for k, v in self._repr_fun.items()]
    splitor = ", " + " " * len(name) + "\n"
    return (f'{name}({splitor.join(format_ref)}, \n' +
            f'{" " * len(name)} num_of_dyn_vars={len(self._dyn_vars)}')


def _get_scan_info(f, dyn_vars, out_vars=None, has_return=False):
  # iterable variables
  if isinstance(dyn_vars, dict):
    dyn_vars = tuple(dyn_vars.values())
  elif isinstance(dyn_vars, (tuple, list)):
    dyn_vars = tuple(dyn_vars)
  else:
    raise ValueError(
      f'"dyn_vars" does not support {type(dyn_vars)}, '
      f'only support dict/list/tuple of {Array.__name__}')
  for v in dyn_vars:
    if not isinstance(v, Array):
      raise ValueError(
        f'brainpy.math.jax.make_loop only support '
        f'{Array.__name__}, but got {type(v)}')

  # outputs
  if out_vars is None:
    out_vars = ()
    _, tree = tree_flatten(out_vars)
  elif isinstance(out_vars, Array):
    _, tree = tree_flatten(out_vars)
    out_vars = (out_vars,)
  elif isinstance(out_vars, dict):
    _, tree = tree_flatten(out_vars)
    out_vars = tuple(out_vars.values())
  elif isinstance(out_vars, (tuple, list)):
    _, tree = tree_flatten(out_vars)
    out_vars = tuple(out_vars)
  else:
    raise ValueError(
      f'"out_vars" does not support {type(out_vars)}, '
      f'only support dict/list/tuple of {Array.__name__}')

  # functions
  if has_return:
    def fun2scan(dyn_values, x):
      for v, d in zip(dyn_vars, dyn_values): v._value = d
      results = f(x)
      dyn_values = [v.value for v in dyn_vars]
      out_values = [v.value for v in out_vars]
      return dyn_values, (out_values, results)

  else:
    def fun2scan(dyn_values, x):
      for v, d in zip(dyn_vars, dyn_values): v._value = d
      f(x)
      dyn_values = [v.value for v in dyn_vars]
      out_values = [v.value for v in out_vars]
      return dyn_values, out_values

  return fun2scan, dyn_vars, tree


def make_loop(
    body_fun: Callable,
    dyn_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]],
    out_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]] = None,
    has_return: bool = False
) -> ControlObject:
  """Make a for-loop function, which iterate over inputs.

  Examples
  --------

  >>> import brainpy.math as bm
  >>>
  >>> a = bm.Variable(bm.zeros(1))
  >>> def f(x): a.value += 1.
  >>> loop = bm.make_loop(f, dyn_vars=[a], out_vars=a)
  >>> loop(bm.arange(10))
  Variable([[ 1.],
            [ 2.],
            [ 3.],
            [ 4.],
            [ 5.],
            [ 6.],
            [ 7.],
            [ 8.],
            [ 9.],
            [10.]], dtype=float32)
  >>> b = bm.Variable(bm.zeros(1))
  >>> def f(x):
  >>>   b.value += 1
  >>>   return b + 1
  >>> loop = bm.make_loop(f, dyn_vars=[b], out_vars=b, has_return=True)
  >>> hist_b, hist_b_plus = loop(bm.arange(10))
  >>> hist_b
  Variable([[ 1.],
            [ 2.],
            [ 3.],
            [ 4.],
            [ 5.],
            [ 6.],
            [ 7.],
            [ 8.],
            [ 9.],
            [10.]], dtype=float32)
  >>> hist_b_plus
  ArrayType([[ 2.],
            [ 3.],
            [ 4.],
            [ 5.],
            [ 6.],
            [ 7.],
            [ 8.],
            [ 9.],
            [10.],
            [11.]], dtype=float32)

  Parameters
  ----------
  body_fun : callable, function
    A function receive one argument. This argument refers to the iterable input ``x``.
  dyn_vars : dict of ArrayType, sequence of ArrayType
    The dynamically changed variables, while iterate between trials.
  out_vars : optional, ArrayType, dict of ArrayType, sequence of ArrayType
    The variables to output their values.
  has_return : bool
    The function has the return values.

  Returns
  -------
  loop_func : ControlObject
    The function for loop iteration. This function receives one argument ``xs``, denoting
    the input tensor which interate over the time (note ``body_fun`` receive ``x``).
  """

  fun2scan, dyn_vars, tree = _get_scan_info(f=body_fun,
                                            dyn_vars=dyn_vars,
                                            out_vars=out_vars,
                                            has_return=has_return)

  # functions
  if has_return:
    def call(xs=None, length=None):
      init_values = [v.value for v in dyn_vars]
      try:
        dyn_values, (out_values, results) = lax.scan(
          f=fun2scan, init=init_values, xs=xs, length=length
        )
      except UnexpectedTracerError as e:
        for v, d in zip(dyn_vars, init_values): v._value = d
        raise errors.JaxTracerError(variables=dyn_vars) from e
      for v, d in zip(dyn_vars, dyn_values): v._value = d
      return tree_unflatten(tree, out_values), results

  else:
    def call(xs):
      init_values = [v.value for v in dyn_vars]
      try:
        dyn_values, out_values = lax.scan(f=fun2scan, init=init_values, xs=xs)
      except UnexpectedTracerError as e:
        for v, d in zip(dyn_vars, init_values): v._value = d
        raise errors.JaxTracerError(variables=dyn_vars) from e
      except Exception as e:
        for v, d in zip(dyn_vars, init_values): v._value = d
        raise e
      for v, d in zip(dyn_vars, dyn_values): v._value = d
      return tree_unflatten(tree, out_values)

  return ControlObject(call, dyn_vars=dyn_vars, repr_fun={'body_fun': body_fun})


def make_while(
    cond_fun,
    body_fun,
    dyn_vars
) -> ControlObject:
  """Make a while-loop function.

  This function is similar to the ``jax.lax.while_loop``. The difference is that,
  if you are using ``Variable`` in your while loop codes, this function will help
  you make an easy while loop function. Note: ``cond_fun`` and ``body_fun`` do no
  receive any arguments. ``cond_fun`` shoule return a boolean value. ``body_fun``
  does not support return values.

  Examples
  --------
  >>> import brainpy.math as bm
  >>>
  >>> a = bm.zeros(1)
  >>>
  >>> def cond_f(x): return a[0] < 10
  >>> def body_f(x): a.value += 1.
  >>>
  >>> loop = bm.make_while(cond_f, body_f, dyn_vars=[a])
  >>> loop()
  >>> a
  Array([10.], dtype=float32)

  Parameters
  ----------
  cond_fun : function, callable
    A function receives one argument, but return a boolean value.
  body_fun : function, callable
    A function receives one argument, without any returns.
  dyn_vars : dict of ArrayType, sequence of ArrayType
    The dynamically changed variables, while iterate between trials.

  Returns
  -------
  loop_func : ControlObject
      The function for loop iteration, which receive one argument ``x`` for external input.
  """
  # iterable variables
  if isinstance(dyn_vars, dict):
    dyn_vars = tuple(dyn_vars.values())
  elif isinstance(dyn_vars, (tuple, list)):
    dyn_vars = tuple(dyn_vars)
  else:
    raise ValueError(f'"dyn_vars" does not support {type(dyn_vars)}, '
                     f'only support dict/list/tuple of {Array.__name__}')
  for v in dyn_vars:
    if not isinstance(v, Array):
      raise ValueError(f'Only support {Array.__name__}, but got {type(v)}')

  def _body_fun(op):
    dyn_values, static_values = op
    for v, d in zip(dyn_vars, dyn_values): v._value = d
    body_fun(static_values)
    return [v.value for v in dyn_vars], static_values

  def _cond_fun(op):
    dyn_values, static_values = op
    for v, d in zip(dyn_vars, dyn_values): v._value = d
    return as_jax(cond_fun(static_values))

  name = get_unique_name('_brainpy_object_oriented_make_while_')

  def call(x=None):
    dyn_init = [v.value for v in dyn_vars]
    try:
      dyn_values, _ = lax.while_loop(cond_fun=_cond_fun,
                                     body_fun=_body_fun,
                                     init_val=(dyn_init, x))
    except UnexpectedTracerError as e:
      for v, d in zip(dyn_vars, dyn_init): v._value = d
      raise errors.JaxTracerError(variables=dyn_vars) from e
    except Exception as e:
      for v, d in zip(dyn_vars, dyn_init): v._value = d
      raise e
    for v, d in zip(dyn_vars, dyn_values): v._value = d

  return ControlObject(call=call,
                       dyn_vars=dyn_vars,
                       repr_fun={'cond_fun': cond_fun, 'body_fun': body_fun},
                       name=name)


def make_cond(
    true_fun,
    false_fun,
    dyn_vars=None
) -> ControlObject:
  """Make a condition (if-else) function.

  Examples
  --------

  >>> import brainpy.math as bm
  >>> a = bm.zeros(2)
  >>> b = bm.ones(2)
  >>>
  >>> def true_f(x):  a.value += 1
  >>> def false_f(x): b.value -= 1
  >>>
  >>> cond = bm.make_cond(true_f, false_f, dyn_vars=[a, b])
  >>> cond(True)
  >>> a, b
  (Array([1., 1.], dtype=float32),
   Array([1., 1.], dtype=float32))
  >>> cond(False)
  >>> a, b
  (Array([1., 1.], dtype=float32),
   Array([0., 0.], dtype=float32))

  Parameters
  ----------
  true_fun : callable, function
    A function receives one argument, without any returns.
  false_fun : callable, function
    A function receives one argument, without any returns.
  dyn_vars : dict of ArrayType, sequence of ArrayType
    The dynamically changed variables.

  Returns
  -------
  cond_func : ControlObject
      The condictional function receives two arguments: ``pred`` for true/false judgement
      and ``x`` for external input.
  """
  # iterable variables
  if dyn_vars is None:
    dyn_vars = []
  if isinstance(dyn_vars, Array):
    dyn_vars = (dyn_vars,)
  elif isinstance(dyn_vars, dict):
    dyn_vars = tuple(dyn_vars.values())
  elif isinstance(dyn_vars, (tuple, list)):
    dyn_vars = tuple(dyn_vars)
  else:
    raise ValueError(f'"dyn_vars" does not support {type(dyn_vars)}, '
                     f'only support dict/list/tuple of {Array.__name__}')
  for v in dyn_vars:
    if not isinstance(v, Array):
      raise ValueError(f'Only support {Array.__name__}, but got {type(v)}')

  name = get_unique_name('_brainpy_object_oriented_make_cond_')

  if len(dyn_vars) > 0:
    def _true_fun(op):
      dyn_vals, static_vals = op
      for v, d in zip(dyn_vars, dyn_vals): v._value = d
      res = true_fun(static_vals)
      dyn_vals = [v.value for v in dyn_vars]
      return dyn_vals, res

    def _false_fun(op):
      dyn_vals, static_vals = op
      for v, d in zip(dyn_vars, dyn_vals): v._value = d
      res = false_fun(static_vals)
      dyn_vals = [v.value for v in dyn_vars]
      return dyn_vals, res

    def call(pred, x=None):
      old_values = [v.value for v in dyn_vars]
      try:
        dyn_values, res = lax.cond(pred, _true_fun, _false_fun, (old_values, x))
      except UnexpectedTracerError as e:
        for v, d in zip(dyn_vars, old_values): v._value = d
        raise errors.JaxTracerError(variables=dyn_vars) from e
      except Exception as e:
        for v, d in zip(dyn_vars, old_values): v._value = d
        raise e
      for v, d in zip(dyn_vars, dyn_values): v._value = d
      return res

  else:
    def call(pred, x=None):
      res = lax.cond(pred, true_fun, false_fun, x)
      return res

  return ControlObject(call, dyn_vars, repr_fun={'true_fun': true_fun, 'false_fun': false_fun})


def _check_f(f):
  if callable(f):
    return f
  else:
    return (lambda _: f)


def _check_sequence(a):
  return isinstance(a, (list, tuple))


def cond(
    pred: bool,
    true_fun: Union[Callable, jnp.ndarray, Array, float, int, bool],
    false_fun: Union[Callable, jnp.ndarray, Array, float, int, bool],
    operands: Any,

    # deprecated
    dyn_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]] = None,
    child_objs: Optional[Union[BrainPyObject, Sequence[BrainPyObject], Dict[str, BrainPyObject]]] = None,
):
  """Simple conditional statement (if-else) with instance of :py:class:`~.Variable`.

  >>> import brainpy.math as bm
  >>> a = bm.Variable(bm.zeros(2))
  >>> b = bm.Variable(bm.ones(2))
  >>> def true_f(_):  a.value += 1
  >>> def false_f(_): b.value -= 1
  >>>
  >>> bm.cond(True, true_f, false_f)
  >>> a, b
  Variable([1., 1.], dtype=float32), Variable([1., 1.], dtype=float32)
  >>>
  >>> bm.cond(False, true_f, false_f)
  >>> a, b
  Variable([1., 1.], dtype=float32), Variable([0., 0.], dtype=float32)

  Parameters
  ----------
  pred: bool
    Boolean scalar type, indicating which branch function to apply.
  true_fun: callable, ArrayType, float, int, bool
    Function to be applied if ``pred`` is True.
    This function must receive one arguement for ``operands``.
  false_fun: callable, ArrayType, float, int, bool
    Function to be applied if ``pred`` is False.
    This function must receive one arguement for ``operands``.
  operands: Any
    Operands (A) input to branching function depending on ``pred``. The type
    can be a scalar, array, or any pytree (nested Python tuple/list/dict) thereof.
  dyn_vars: optional, Variable, sequence of Variable, dict
    The dynamically changed variables.
  child_objs: optional, dict, sequence of BrainPyObject, BrainPyObject
    The children objects used in the target function.

    .. versionadded:: 2.3.1

  Returns
  -------
  res: Any
    The conditional results.
  """

  # functions
  true_fun = _check_f(true_fun)
  false_fun = _check_f(false_fun)

  # operands
  if not isinstance(operands, (tuple, list)):
    operands = (operands,)

  # dyn vars
  dynvar_deprecation(dyn_vars)
  node_deprecation(child_objs)

  if jax.config.jax_disable_jit:
    dyn_vars = VariableStack()

  else:
    dyn_vars = evaluate_dyn_vars(true_fun, *operands)
    dyn_vars += evaluate_dyn_vars(false_fun, *operands)

  # TODO: cache mechanism?
  if len(dyn_vars) > 0:
    def _true_fun(dyn_vals, *static_vals):
      for k, v in dyn_vars.items():
        v._value = dyn_vals[k]
      r = true_fun(*static_vals)
      return {k: v.value for k, v in dyn_vars.items()}, r

    def _false_fun(dyn_vals, *static_vals):
      for k, v in dyn_vars.items():
        v._value = dyn_vals[k]
      r = false_fun(*static_vals)
      return {k: v.value for k, v in dyn_vars.items()}, r

    old_values = {k: v.value for k, v in dyn_vars.items()}
    dyn_values, res = lax.cond(pred, _true_fun, _false_fun, old_values, *operands)
    for k, v in dyn_vars.items():
      v._value = dyn_values[k]
  else:
    res = lax.cond(pred, true_fun, false_fun, *operands)
  return res


def ifelse(
    conditions: Union[bool, Sequence[bool]],
    branches: Sequence[Any],
    operands: Any = None,
    show_code: bool = False,

    # deprecated
    dyn_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]] = None,
    child_objs: Optional[Union[BrainPyObject, Sequence[BrainPyObject], Dict[str, BrainPyObject]]] = None,
):
  """``If-else`` control flows looks like native Pythonic programming.

  Examples
  --------

  >>> import brainpy.math as bm
  >>> def f(a):
  >>>    return bm.ifelse(conditions=[a > 10, a > 5, a > 2, a > 0],
  >>>                     branches=[lambda _: 1,
  >>>                               lambda _: 2,
  >>>                               lambda _: 3,
  >>>                               lambda _: 4,
  >>>                               lambda _: 5])
  >>> f(1)
  4
  >>> # or, it can be expressed as:
  >>> def f(a):
  >>>   return bm.ifelse(conditions=[a > 10, a > 5, a > 2, a > 0],
  >>>                    branches=[1, 2, 3, 4, 5])
  >>> f(3)
  3

  Parameters
  ----------
  conditions: bool, sequence of bool
    The boolean conditions.
  branches: Any
    The branches, at least has two elements. Elements can be functions,
    arrays, or numbers. The number of ``branches`` and ``conditions`` has
    the relationship of `len(branches) == len(conditions) + 1`.
    Each branch should receive one arguement for ``operands``.
  operands: optional, Any
    The operands for each branch.
  dyn_vars: Variable, sequence of Variable, dict
    The dynamically changed variables.
  show_code: bool
    Whether show the formatted code.
  child_objs: optional, dict, sequence of BrainPyObject, BrainPyObject
    The children objects used in the target function.

    .. versionadded:: 2.3.1

  Returns
  -------
  res: Any
    The results of the control flow.
  """
  # checking
  if not isinstance(conditions, (tuple, list)):
    conditions = [conditions]
  if not isinstance(conditions, (tuple, list)):
    raise ValueError(f'"conditions" must be a tuple/list of boolean values. '
                     f'But we got {type(conditions)}: {conditions}')
  if not isinstance(branches, (tuple, list)):
    raise ValueError(f'"branches" must be a tuple/list. '
                     f'But we got {type(branches)}.')
  branches = [_check_f(b) for b in branches]
  if len(branches) != len(conditions) + 1:
    raise ValueError(f'The numbers of branches and conditions do not match. '
                     f'Got len(conditions)={len(conditions)} and len(branches)={len(branches)}. '
                     f'We expect len(conditions) + 1 == len(branches). ')

  dynvar_deprecation(dyn_vars)
  node_deprecation(child_objs)

  # format new codes
  if len(conditions) == 1:
    return cond(conditions[0],
                branches[0],
                branches[1],
                operands)
  else:
    code_scope = {'conditions': conditions, 'branches': branches}
    codes = ['def f(operands):',
             f'  f0 = branches[{len(conditions)}]']
    num_cond = len(conditions) - 1
    code_scope['_cond'] = cond
    for i in range(len(conditions) - 1):
      codes.append(f'  f{i + 1} = lambda r: _cond(conditions[{num_cond - i}], branches[{num_cond - i}], f{i}, r)')
    codes.append(f'  return _cond(conditions[0], branches[0], f{len(conditions) - 1}, operands)')
    codes = '\n'.join(codes)
    if show_code: print(codes)
    exec(compile(codes.strip(), '', 'exec'), code_scope)
    f = code_scope['f']
    r = f(operands)
    return r


def _loop_abstractify(x):
  x = abstract(x)
  return jax.core.mapped_aval(x.shape[0], 0, x)


def for_loop(
    body_fun: Callable,
    operands: Any,
    reverse: bool = False,
    unroll: int = 1,
    remat: bool = False,
    jit: bool = True,
    progress_bar: bool = False,

    # deprecated
    dyn_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]] = None,
    child_objs: Optional[Union[BrainPyObject, Sequence[BrainPyObject], Dict[str, BrainPyObject]]] = None,
):
  """``for-loop`` control flow with :py:class:`~.Variable`.

  .. versionadded:: 2.1.11

  .. versionchanged:: 2.3.0
     ``dyn_vars`` has been changed into a default argument.
     Please change your call from ``for_loop(fun, dyn_vars, operands)``
     to ``for_loop(fun, operands, dyn_vars)``.

  Simply speaking, all dynamically changed variables used in the body function should
  be labeld in ``dyn_vars`` argument. All returns in body function will be gathered
  as the return of the whole loop.

  >>> import brainpy.math as bm
  >>> a = bm.Variable(bm.zeros(1))
  >>> b = bm.Variable(bm.ones(1))
  >>> # first example
  >>> def body(x):
  >>>    a.value += x
  >>>    b.value *= x
  >>>    return a.value
  >>> a_hist = bm.for_loop(body, operands=bm.arange(1, 5))
  >>> a_hist
  DeviceArray([[ 1.],
               [ 3.],
               [ 6.],
               [10.]], dtype=float32)
  >>> a
  Variable([10.], dtype=float32)
  >>> b
  Variable([24.], dtype=float32)
  >>>
  >>> # another example
  >>> def body(x, y):
  >>>   a.value += x
  >>>   b.value *= y
  >>>   return a.value
  >>> a_hist = bm.for_loop(body, operands=(bm.arange(1, 5), bm.arange(2, 6)))
  >>> a_hist
  [[11.]
   [13.]
   [16.]
   [20.]]

  Parameters
  ----------
  body_fun: callable
    A Python function to be scanned. This function accepts one argument and returns one output.
    The argument denotes a slice of ``operands`` along its leading axis, and that
    output represents a slice of the return value.
  operands: Any
    The value over which to scan along the leading axis,
    where ``operands`` can be an array or any pytree (nested Python
    tuple/list/dict) thereof with consistent leading axis sizes.
    If body function `body_func` receives multiple arguments,
    `operands` should be a tuple/list whose length is equal to the
    number of arguments.
  remat: bool
    Make ``fun`` recompute internal linearization points when differentiated.
  jit: bool
    Whether to just-in-time compile the function.
  reverse: bool
    Optional boolean specifying whether to run the scan iteration
    forward (the default) or in reverse, equivalent to reversing the leading
    axes of the arrays in both ``xs`` and in ``ys``.
  unroll: int
    Optional positive int specifying, in the underlying operation of the
    scan primitive, how many scan iterations to unroll within a single
    iteration of a loop.
  progress_bar: bool
    Whether we use the progress bar to report the running progress.

  dyn_vars: Variable, sequence of Variable, dict
    The instances of :py:class:`~.Variable`.

    .. deprecated:: 2.4.0
       No longer need to provide ``dyn_vars``. This function is capable of automatically
       collecting the dynamical variables used in the target ``func``.
  child_objs: optional, dict, sequence of BrainPyObject, BrainPyObject
    The children objects used in the target function.

    .. versionadded:: 2.3.1

    .. deprecated:: 2.4.0
       No longer need to provide ``child_objs``. This function is capable of automatically
       collecting the children objects used in the target ``func``.

  Returns
  -------
  outs: Any
    The stacked outputs of ``body_fun`` when scanned over the leading axis of the inputs.
  """

  dynvar_deprecation(dyn_vars)
  node_deprecation(child_objs)

  if not isinstance(operands, (list, tuple)):
    operands = (operands,)

  if progress_bar:
    num_total = min([op.shape[0] for op in jax.tree_util.tree_flatten(operands)[0]])
    bar = tqdm(total=num_total)

  dyn_vars = get_stack_cache(body_fun)
  if not jit:
    if dyn_vars is None:
      dyn_vars = VariableStack()

  else:
    # TODO: better cache mechanism?
    if dyn_vars is None:
      with jax.ensure_compile_time_eval():
        op_vals = jax.tree_util.tree_map(_loop_abstractify, operands)
        with VariableStack() as dyn_vars:
          _ = jax.eval_shape(body_fun, *op_vals)
        cache_stack(body_fun, dyn_vars)  # cache
        del op_vals

  # functions
  def fun2scan(carry, x):
    for k in dyn_vars.keys():
      dyn_vars[k]._value = carry[k]
    results = body_fun(*x)
    if progress_bar:
      id_tap(lambda *arg: bar.update(), ())
    return dyn_vars.dict_data(), results

  if remat:
    fun2scan = jax.checkpoint(fun2scan)

  # TODO: cache mechanism?
  with jax.disable_jit(not jit):
    dyn_vals, out_vals = lax.scan(f=fun2scan,
                                  init=dyn_vars.dict_data(),
                                  xs=operands,
                                  reverse=reverse,
                                  unroll=unroll)
  for key in dyn_vars.keys():
    dyn_vars[key]._value = dyn_vals[key]
  if progress_bar:
    bar.close()
  return out_vals


def scan(
   f: Callable,
   init: Any,
   xs: Any,
   length: Optional[int] = None,
   reverse: bool = False,
   unroll: int = 1
):
  jax.lax.scan



def while_loop(
    body_fun: Callable,
    cond_fun: Callable,
    operands: Any,

    # deprecated
    dyn_vars: Union[Variable, Sequence[Variable], Dict[str, Variable]] = None,
    child_objs: Optional[Union[BrainPyObject, Sequence[BrainPyObject], Dict[str, BrainPyObject]]] = None,
):
  """``while-loop`` control flow with :py:class:`~.Variable`.

  .. versionchanged:: 2.3.0
     ``dyn_vars`` has been changed into a default argument.
     Please change your call from ``while_loop(f1, f2, dyn_vars, operands)``
     to ``while_loop(f1, f2, operands, dyn_vars)``.

  Note the diference between ``for_loop`` and ``while_loop``:

  1. ``while_loop`` does not support accumulating history values.
  2. The returns on the body function of ``for_loop`` represent the values to stack at one moment.
     However, the functional returns of body function in ``while_loop`` represent the operands'
     values at the next moment, meaning that the body function of ``while_loop`` defines the
     updating rule of how the operands are updated.

  >>> import brainpy.math as bm
  >>>
  >>> a = bm.Variable(bm.zeros(1))
  >>> b = bm.Variable(bm.ones(1))
  >>>
  >>> def cond(x, y):
  >>>    return x < 6.
  >>>
  >>> def body(x, y):
  >>>    a.value += x
  >>>    b.value *= y
  >>>    return x + b[0], y + 1.
  >>>
  >>> res = bm.while_loop(body, cond, operands=(1., 1.))
  >>> res
  (10.0, 4.0)

  .. versionadded:: 2.1.11

  Parameters
  ----------
  body_fun: callable
    A function which define the updating logic. It receives one argument for ``operands``, without returns.
  cond_fun: callable
    A function which define the stop condition. It receives one argument for ``operands``,
    with one boolean value return.
  operands: Any
    The operands for ``body_fun`` and ``cond_fun`` functions.
  dyn_vars: Variable, sequence of Variable, dict
    The dynamically changed variables.

    .. deprecated:: 2.4.0
       No longer need to provide ``dyn_vars``. This function is capable of automatically
       collecting the dynamical variables used in the target ``func``.
  child_objs: optional, dict, sequence of BrainPyObject, BrainPyObject
    The children objects used in the target function.

    .. versionadded:: 2.3.1

    .. deprecated:: 2.4.0
       No longer need to provide ``child_objs``. This function is capable of automatically
       collecting the children objects used in the target ``func``.


  """
  dynvar_deprecation(dyn_vars)
  node_deprecation(child_objs)

  if not isinstance(operands, (list, tuple)):
    operands = (operands,)

  if jax.config.jax_disable_jit:
    dyn_vars = VariableStack()

  else:
    dyn_vars = evaluate_dyn_vars(body_fun, *operands)
    dyn_vars += evaluate_dyn_vars(cond_fun, *operands)

  def _body_fun(op):
    dyn_vals, old_vals = op
    for k, v in dyn_vars.items():
      v._value = dyn_vals[k]
    new_vals = body_fun(*old_vals)
    if new_vals is None:
      new_vals = tuple()
    if not isinstance(new_vals, tuple):
      new_vals = (new_vals,)
    return dyn_vars.dict_data(), new_vals

  def _cond_fun(op):
    dyn_vals, old_vals = op
    for k, v in dyn_vars.items():
      v._value = dyn_vals[k]
    r = cond_fun(*old_vals)
    return r if isinstance(r, Array) else r

  # TODO: cache mechanism?
  dyn_values, out = lax.while_loop(cond_fun=_cond_fun,
                                   body_fun=_body_fun,
                                   init_val=(dyn_vars.dict_data(), operands))
  for k, v in dyn_vars.items():
    v._value = dyn_values[k]
  return out
