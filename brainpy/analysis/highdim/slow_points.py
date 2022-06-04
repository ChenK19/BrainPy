# -*- coding: utf-8 -*-

import time
import warnings
from typing import Callable, Union, Dict, Optional, Sequence

import jax.numpy as jnp
import numpy as np
from jax import vmap
from jax.scipy.optimize import minimize
from jax.tree_util import tree_flatten, tree_unflatten, tree_map

import brainpy.math as bm
from brainpy import optimizers as optim, losses
from brainpy.analysis import utils, base, constants
from brainpy.base import TensorCollector
from brainpy.dyn.base import DynamicalSystem
from brainpy.dyn.runners import build_inputs, check_and_format_inputs
from brainpy.errors import AnalyzerError, UnsupportedError
from brainpy.types import Tensor

__all__ = [
  'SlowPointFinder',
]

F_OPT_SOLVER = 'function_for_opt_solver'
F_GRADIENT_DESCENT = 'function_for_gradient_descent'

SUPPORTED_OPT_SOLVERS = {
  'BFGS': lambda f, x0: minimize(f, x0, method='BFGS')
}


class SlowPointFinder(base.BrainPyAnalyzer):
  """Find fixed/slow points by numerical optimization.

  This class can help you:

  - optimize to find the closest fixed points / slow points
  - exclude any fixed points whose fixed point loss is above threshold
  - exclude any non-unique fixed points according to a tolerance
  - exclude any far-away "outlier" fixed points

  Parameters
  ----------
  f_cell : callable, function, DynamicalSystem
    The target of computing the recurrent units.

  f_type : str
    The system's type: continuous system or discrete system.

    - 'continuous': continuous derivative function, denotes this is a continuous system, or
    - 'discrete': discrete update function, denotes this is a discrete system.

  verbose : bool
    Whether output the optimization progress.

  f_loss: callable
    The loss function.
    - If ``f_type`` is `"discrete"`, the loss function must receive three arguments, i.e.,
      ``loss(outputs, targets, axis)``.
    - If ``f_type`` is `"continuous"`, the loss function must receive two arguments, i.e.,
      ``loss(outputs, axis)``.

    .. versionadded:: 2.2.0

  t: float
    The time to evaluate the fixed points.

    .. versionadded:: 2.2.0

  inputs: sequence
    Same as ``inputs`` in :py:class:`~.DSRunner`.

    .. versionadded:: 2.2.0

  excluded_vars: sequence
    The excluded variables (can be a sequence of  `Variable` instances),
    when ``f_cell`` is an instance of :py:class:`~.DynamicalSystem`.
    These variables will not be included for optimization of fixed points.

    .. versionadded:: 2.2.0

  included_vars: dict
    The target variables (can be a dict of `Variable` instances),
    when ``f_cell`` is an instance of :py:class:`~.DynamicalSystem`.
    These variables will be included for optimization of fixed points.
    The candidate points later provided should have same keys as in ``included_vars``.

    .. versionadded:: 2.2.0

  f_loss_batch : callable, function
    The function to compute the loss.

    .. deprecated:: 2.2.0
       Has been removed. Please use ``f_loss`` to set different loss function.

  """

  def __init__(
      self,
      f_cell: Union[Callable, DynamicalSystem],
      f_type: str = None,
      f_loss: Callable = None,
      inputs: Sequence = None,
      t: float = 0.,
      verbose: bool = True,
      f_loss_batch: Callable = None,
      included_vars: Dict[str, bm.Variable] = None,
      excluded_vars: Sequence[bm.Variable] = (),
  ):
    super(SlowPointFinder, self).__init__()

    # update function
    if included_vars is None:
      self.included_vars = TensorCollector()
    else:
      if not isinstance(included_vars, dict):
        raise TypeError(f'"included_vars" must be a dict but we got {type(included_vars)}')
      self.included_vars = TensorCollector(included_vars)
    if not isinstance(excluded_vars, (tuple, list)):
      raise TypeError(f'"excluded_vars" must be a sequence but we got {type(excluded_vars)}')
    for v in excluded_vars:
      if not isinstance(v, bm.Variable):
        raise TypeError(f'"excluded_vars" must be a sequence of Variable, '
                        f'but we got {type(v)}')
    self.excluded_vars = {f'_exclude_v{i}': v for i, v in enumerate(excluded_vars)}
    self.target = f_cell

    if len(self.included_vars) > 0 and len(self.excluded_vars) > 0:
      raise ValueError

    if isinstance(f_cell, DynamicalSystem):
      # included variables
      all_vars = f_cell.vars(method='relative', level=-1, include_self=True).unique()
      # exclude variables
      if len(self.included_vars) > 0:
        _all_ids = [id(v) for v in self.included_vars.values()]
        for k, v in all_vars.items():
          if id(v) not in _all_ids:
            self.excluded_vars[k] = v
      else:
        self.included_vars = all_vars
        if len(excluded_vars):
          excluded_vars = [id(v) for v in excluded_vars]
          for key, val in tuple(self.included_vars.items()):
            if id(val) in excluded_vars:
              self.included_vars.pop(key)
      # input function
      if inputs is not None:
        inputs = check_and_format_inputs(host=self.target, inputs=inputs)
        _input_step, _i = build_inputs(inputs)
        if _i is not None:
          raise UnsupportedError(f'Do not support iterable inputs when using fixed point finder.')
      else:
        _input_step = None
      # update function
      self.f_cell = self._generate_ds_cell_function(self.target,
                                                    self.included_vars,
                                                    self.excluded_vars,
                                                    t,
                                                    _input_step)
      # check function type
      if f_type is not None:
        if f_type != constants.DISCRETE:
          raise ValueError(f'"f_type" must be "{constants.DISCRETE}" when "f_cell" '
                           f'is instance of {DynamicalSystem.__name__}')
      f_type = constants.DISCRETE

    elif callable(f_cell):
      self.f_cell = f_cell
      if inputs is not None:
        raise UnsupportedError('Do not support "inputs" when "f_cell" is not instance of '
                               f'{DynamicalSystem.__name__}')

    else:
      raise ValueError(f'Unknown type of "f_type": {type(f_cell)}')
    if f_type not in [constants.DISCRETE, constants.CONTINUOUS]:
      raise AnalyzerError(f'Only support "{constants.CONTINUOUS}" (continuous derivative function) or '
                          f'"{constants.DISCRETE}" (discrete update function), not {f_type}.')
    self.verbose = verbose
    self.f_type = f_type

    # loss functon
    if f_loss_batch is not None:
      raise UnsupportedError('"f_loss_batch" is no longer supported, please '
                             'use "f_loss" instead.')
    if f_loss is None:
      f_loss = losses.mean_squared_error if f_type == constants.DISCRETE else losses.mean_square
    self.f_loss = f_loss

    # functions
    self._opt_functions = dict()

    # functions
    if self.f_type == constants.DISCRETE:
      # evaluate losses of a batch of inputs
      self.f_eval_loss = bm.jit(lambda h: self.f_loss(h, vmap(self.f_cell)(h), axis=1))
    else:
      # evaluate losses of a batch of inputs
      self.f_eval_loss = bm.jit(lambda h: self.f_loss(vmap(self.f_cell)(h), axis=1))
    # evaluate Jacobian matrix of a batch of inputs

    # if f_type == constants.DISCRETE:
    #   # overall loss function for fixed points optimization
    #   self.f_loss = bm.jit(lambda h: f_loss(h, f_cell(h)))
    #   # evaluate losses of a batch of inputs
    #   self.f_loss_batch = bm.jit(lambda h: f_loss(h, vmap(f_cell)(h), axis=1))
    # elif f_type == constants.CONTINUOUS:
    #   # overall loss function for fixed points optimization
    #   self.f_loss = bm.jit(lambda h: f_loss(f_cell(h)))
    #   # evaluate losses of a batch of inputs
    #   self.f_loss_batch = bm.jit(lambda h: f_loss(vmap(f_cell)(h), axis=1))

    # essential variables
    self._losses = None
    self._fixed_points = None
    self._selected_ids = None
    self._opt_losses = None

  @property
  def opt_losses(self) -> np.ndarray:
    """The optimization losses."""
    return np.asarray(self._opt_losses)

  @opt_losses.setter
  def opt_losses(self, val):
    raise UnsupportedError('Do not support set "opt_losses" by users.')

  @property
  def fixed_points(self) -> Union[np.ndarray, Dict[str, np.ndarray]]:
    """The final fixed points found."""
    return tree_map(lambda a: np.asarray(a), self._fixed_points)

  @fixed_points.setter
  def fixed_points(self, val):
    raise UnsupportedError('Do not support set "fixed_points" by users.')

  @property
  def num_fps(self) -> int:
    if isinstance(self._fixed_points, dict):
      return tuple(self._fixed_points.values())[0].shape[0]
    else:
      return self._fixed_points.shape[0]

  @property
  def losses(self) -> np.ndarray:
    """Losses of fixed points."""
    return np.asarray(self._losses)

  @losses.setter
  def losses(self, val):
    raise UnsupportedError('Do not support set "losses" by users.')

  @property
  def selected_ids(self) -> np.ndarray:
    """The selected ids of candidate points."""
    return np.asarray(self._selected_ids)

  @selected_ids.setter
  def selected_ids(self, val):
    raise UnsupportedError('Do not support set "selected_ids" by users.')


  def find_fps_with_gd_method(
      self,
      candidates: Union[Tensor, Dict[str, Tensor]],
      tolerance: Union[float, Dict[str, float]] = 1e-5,
      num_batch: int = 100,
      num_opt: int = 10000,
      optimizer: optim.Optimizer = None,
      opt_setting: Optional[Dict] = None
  ):
    """Optimize fixed points with gradient descent methods.

    Parameters
    ----------
    candidates : Tensor, dict
      The array with the shape of (batch size, state dim) of hidden states
      of RNN to start training for fixed points.

    tolerance: float
      The loss threshold during optimization

    num_opt : int
      The maximum number of optimization.

    num_batch : int
      Print training information during optimization every so often.

    opt_setting: optional, dict
      The optimization settings.

      .. deprecated:: 2.1.2
         Use "optimizer" to set optimization method instead.

    optimizer: optim.Optimizer
      The optimizer instance.

      .. versionadded:: 2.1.2
    """

    # optimization settings
    if opt_setting is None:
      if optimizer is None:
        optimizer = optim.Adam(lr=optim.ExponentialDecay(0.2, 1, 0.9999),
                               beta1=0.9, beta2=0.999, eps=1e-8)
      else:
        if not isinstance(optimizer, optim.Optimizer):
          raise ValueError(f'Must be an instance of {optim.Optimizer.__name__}, '
                           f'while we got {type(optimizer)}')
    else:
      warnings.warn('\nPlease use "optimizer" to set optimization method. '
                    '"opt_setting" is deprecated since version 2.1.2. ',
                    UserWarning)

      assert isinstance(opt_setting, dict)
      assert 'method' in opt_setting
      assert 'lr' in opt_setting
      opt_method = opt_setting.pop('method')
      if isinstance(opt_method, str):
        assert opt_method in optim.__dict__
        opt_method = getattr(optim, opt_method)
      assert issubclass(opt_method, optim.Optimizer)
      opt_lr = opt_setting.pop('lr')
      assert isinstance(opt_lr, (int, float, optim.Scheduler))
      opt_setting = opt_setting
      optimizer = opt_method(lr=opt_lr, **opt_setting)

    # set up optimization
    num_candidate = self._check_candidates(candidates)
    if not (isinstance(candidates, (bm.ndarray, jnp.ndarray, np.ndarray)) or isinstance(candidates, dict)):
      raise ValueError('Candidates must be instance of JaxArray or dict of JaxArray.')
    leaves, tree = tree_flatten(candidates, is_leaf=lambda x: isinstance(x, bm.JaxArray))
    fixed_points = tree_unflatten(tree, [bm.TrainVar(leaf) for leaf in leaves])

    def f_loss():
      return self.f_eval_loss(tree_map(lambda a: a.value,
                                       fixed_points,
                                       is_leaf=lambda x: isinstance(x, bm.JaxArray))).mean()

    grad_f = bm.grad(f_loss, grad_vars=fixed_points, return_value=True)
    optimizer.register_vars(fixed_points if isinstance(fixed_points, dict) else {'a': fixed_points})
    dyn_vars = optimizer.vars() + (fixed_points if isinstance(fixed_points, dict) else {'a': fixed_points})
    dyn_vars = dyn_vars.unique()

    def train(idx):
      gradients, loss = grad_f()
      optimizer.update(gradients if isinstance(gradients, dict) else {'a': gradients})
      return loss

    def batch_train(start_i, n_batch):
      f = bm.make_loop(train, dyn_vars=dyn_vars, has_return=True)
      return f(bm.arange(start_i, start_i + n_batch))

    # Run the optimization
    if self.verbose:
      print(f"Optimizing with {optimizer} to find fixed points:")
    opt_losses = []
    do_stop = False
    num_opt_loops = int(num_opt / num_batch)
    for oidx in range(num_opt_loops):
      if do_stop:
        break
      batch_idx_start = oidx * num_batch
      start_time = time.time()
      (_, train_losses) = batch_train(start_i=batch_idx_start, n_batch=num_batch)
      batch_time = time.time() - start_time
      opt_losses.append(train_losses)

      if self.verbose:
        print(f"    "
              f"Batches {batch_idx_start + 1}-{batch_idx_start + num_batch} "
              f"in {batch_time:0.2f} sec, Training loss {train_losses[-1]:0.10f}")

      if train_losses[-1] < tolerance:
        do_stop = True
        if self.verbose:
          print(f'    '
                f'Stop optimization as mean training loss {train_losses[-1]:0.10f} '
                f'is below tolerance {tolerance:0.10f}.')

    self._opt_losses = bm.concatenate(opt_losses)
    self._losses = self.f_eval_loss(tree_map(lambda a: a.value,
                                             fixed_points,
                                             is_leaf=lambda x: isinstance(x, bm.JaxArray)))
    self._fixed_points = tree_map(lambda a: a.value, fixed_points,
                                  is_leaf=lambda x: isinstance(x, bm.JaxArray))
    self._selected_ids = jnp.arange(num_candidate)

  def find_fps_with_opt_solver(
      self,
      candidates: Union[Tensor, Dict[str, Tensor]],
      opt_solver: str = 'BFGS'
  ):
    """Optimize fixed points with nonlinear optimization solvers.

    Parameters
    ----------
    candidates: Tensor, dict
      The candidate (initial) fixed points.
    opt_solver: str
      The solver of the optimization.
    """

    # optimization function
    num_candidate = self._check_candidates(candidates)
    for var in self.included_vars.values():
      if bm.ndim(var) != 1:
        raise ValueError('Cannot use opt solver.')
    if self._opt_functions.get(F_OPT_SOLVER, None) is None:
      self._opt_functions[F_OPT_SOLVER] = self._get_f_for_opt_solver(
        candidates, SUPPORTED_OPT_SOLVERS[opt_solver])
    f_opt = self._opt_functions[F_OPT_SOLVER]

    if self.verbose:
      print(f"Optimizing with {opt_solver} to find fixed points:")

    # optimizing
    res = f_opt(tree_map(lambda a: a.value,
                         candidates,
                         is_leaf=lambda a: isinstance(a, bm.JaxArray)))

    # results
    valid_ids = jnp.where(res.success)[0]
    fixed_points = res.x[valid_ids]
    if isinstance(candidates, dict):
      indices = [0]
      for v in candidates.values():
        indices.append(v.shape[1])
      indices = np.cumsum(indices)
      keys = tuple(candidates.keys())
      self._fixed_points = {key: fixed_points[:, indices[i]: indices[i + 1]]
                            for i, key in enumerate(keys)}
    else:
      self._fixed_points = fixed_points
    self._losses = res.fun[valid_ids]
    self._selected_ids = jnp.asarray(valid_ids)
    if self.verbose:
      print(f'    '
            f'Found {len(valid_ids)} fixed points from {num_candidate} initial points.')

  def filter_loss(self, tolerance: float = 1e-5):
    """Filter fixed points whose speed larger than a given tolerance.

    Parameters
    ----------
    tolerance: float
      Discard fixed points with squared speed larger than this value.
    """
    if self.verbose:
      print(f"Excluding fixed points with squared speed above "
            f"tolerance {tolerance}:")
    if isinstance(self._fixed_points, dict):
      num_fps = tuple(self._fixed_points.values())[0].shape[0]
    else:
      num_fps = self._fixed_points.shape[0]
    ids = self._losses < tolerance
    keep_ids = bm.as_device_array(bm.where(ids)[0])
    self._fixed_points = tree_map(lambda a: a[keep_ids], self._fixed_points)
    self._losses = self._losses[keep_ids]
    self._selected_ids = self._selected_ids[keep_ids]
    if self.verbose:
      print(f"    "
            f"Kept {len(keep_ids)}/{num_fps} "
            f"fixed points with tolerance under {tolerance}.")

  def keep_unique(self, tolerance: float = 2.5e-2):
    """Filter unique fixed points by choosing a representative within tolerance.

    Parameters
    ----------
    tolerance: float
      Tolerance for determination of identical fixed points.
    """
    if self.verbose:
      print("Excluding non-unique fixed points:")
    if isinstance(self._fixed_points, dict):
      num_fps = tuple(self._fixed_points.values())[0].shape[0]
    else:
      num_fps = self._fixed_points.shape[0]
    fps, keep_ids = utils.keep_unique(self.fixed_points, tolerance=tolerance)
    self._fixed_points = tree_map(lambda a: jnp.asarray(a), fps)
    self._losses = self._losses[keep_ids]
    self._selected_ids = self._selected_ids[keep_ids]
    if self.verbose:
      print(f"    Kept {keep_ids.shape[0]}/{num_fps} unique fixed points "
            f"with uniqueness tolerance {tolerance}.")

  def exclude_outliers(self, tolerance: float = 1e0):
    """Exclude points whose closest neighbor is further than threshold.

    Parameters
    ----------
    tolerance: float
      Any point whose closest fixed point is greater than tol is an outlier.
    """
    if self.verbose:
      print("Excluding outliers:")
    if np.isinf(tolerance):
      return
    if isinstance(self._fixed_points, dict):
      num_fps = tuple(self._fixed_points.values())[0].shape[0]
    else:
      num_fps = self._fixed_points.shape[0]
    if num_fps <= 1:
      return

    # Compute pairwise distances between all fixed points.
    distances = np.asarray(utils.euclidean_distance_jax(self.fixed_points, num_fps))

    # Find second smallest element in each column of the pairwise distance matrix.
    # This corresponds to the closest neighbor for each fixed point.
    closest_neighbor = np.partition(distances, kth=1, axis=0)[1]

    # Return data with outliers removed and indices of kept datapoints.
    keep_ids = np.where(closest_neighbor < tolerance)[0]
    self._fixed_points = tree_map(lambda a: a[keep_ids], self._fixed_points)
    self._selected_ids = self._selected_ids[keep_ids]
    self._losses = self._losses[keep_ids]

    if self.verbose:
      print(f"    "
            f"Kept {keep_ids.shape[0]}/{num_fps} fixed points "
            f"with within outlier tolerance {tolerance}.")

  def compute_jacobians(self, points, stack_vars=True):
    """Compute the jacobian matrices at the points.

    Parameters
    ----------
    points: np.ndarray, bm.JaxArray, jax.ndarray
      The fixed points with the shape of (num_point, num_dim).
    stack_vars: bool
    """
    ndim = np.unique([l.ndim for l in tree_flatten(points, is_leaf=lambda a: isinstance(a, bm.JaxArray))[0]])
    if len(ndim) != 1:
      raise ValueError(f'Get multiple dimension of the evaluated points. {ndim}')
    if ndim[0] == 1:
      points = tree_map(lambda a: bm.asarray([a]), points)
    elif ndim[0] == 2:
      pass
    else:
      raise ValueError('Only support points of 1D: (num_feature,) or 2D: (num_point, num_feature)')

    if isinstance(points, dict) and stack_vars:
      points = bm.hstack(points.values()).value
    return self._get_f_jocabian(stack_vars)(points)

  @staticmethod
  def decompose_eigenvalues(matrices, sort_by='magnitude', do_compute_lefts=True):
    """Compute the eigenvalues of the matrices.

    Parameters
    ----------
    matrices: np.ndarray, bm.JaxArray, jax.ndarray
      A 3D array with the shape of (num_matrices, dim, dim).
    sort_by: str
      The method of sorting.
    do_compute_lefts: bool
      Compute the left eigenvectors? Requires a pseudo-inverse call.

    Returns
    -------
    decompositions : list
      A list of dictionaries with sorted eigenvalues components:
      (eigenvalues, right eigenvectors, and left eigenvectors).
    """
    if sort_by == 'magnitude':
      sort_fun = np.abs
    elif sort_by == 'real':
      sort_fun = np.real
    else:
      raise ValueError("Not implemented yet.")
    matrices = np.asarray(matrices)

    decompositions = []
    for mat in matrices:
      eig_values, eig_vectors = np.linalg.eig(mat)
      indices = np.flipud(np.argsort(sort_fun(eig_values)))
      L = None
      if do_compute_lefts:
        L = np.linalg.pinv(eig_vectors).T  # as columns
        L = L[:, indices]
      decompositions.append({'eig_values': eig_values[indices],
                             'R': eig_vectors[:, indices],
                             'L': L})
    return decompositions

  def _get_f_for_opt_solver(self, candidates, opt_method):
    # update function
    if isinstance(candidates, (bm.ndarray, jnp.ndarray, np.ndarray)):
      f_cell = self.f_cell

    elif isinstance(candidates, dict):
      indices = [0]
      for v in self.included_vars.values():
        indices.append(v.shape[0])
      indices = np.cumsum(indices)
      keys = tuple(self.included_vars.keys())

      def f_cell(x):
        x = {keys[i]: x[indices[i]: indices[i + 1]] for i in range(len(keys))}
        r = self.f_cell(x)
        return r

    else:
      raise ValueError(f'Only supports tensor or a dict of tensors. But we got {type(candidates)}')

    # loss function
    if self.f_type == constants.DISCRETE:
      # overall loss function for fixed points optimization
      if isinstance(candidates, dict):
        def f_loss(h):
          return bm.as_device_array(
            self.f_loss({key: h[indices[i]: indices[i + 1]] for i, key in enumerate(self.included_vars.keys())},
                        {k: v for k, v in f_cell(h).items() if k in self.included_vars})
          )
      else:
        def f_loss(h):
          return bm.as_device_array(self.f_loss(h, f_cell(h)))
    else:
      # overall loss function for fixed points optimization
      def f_loss(h):
        return self.f_loss(f_cell(h))

    excluded_data = {k: v.value for k, v in self.excluded_vars.items()}

    @bm.jit
    @vmap
    def f_opt(x0):
      for k, v in self.included_vars.items():
        v.value = x0[k]
      for k, v in self.excluded_vars.items():
        v.value = excluded_data[k]
      if isinstance(x0, dict):
        x0 = bm.concatenate(tuple(x0.values())).value
      return opt_method(f_loss, x0)

    return f_opt

  def _generate_ds_cell_function(self,
                                 ds_instance,
                                 included_vars: Dict,
                                 excluded_vars: Dict,
                                 t=0.,
                                 f_input=None):

    excluded_data = {k: v.value for k, v in excluded_vars.items()}

    def f_cell(h: Dict):
      for k, v in included_vars.items():
        v.value = bm.asarray(h[k], dtype=v.dtype)
      for k, v in excluded_vars.items():
        v.value = excluded_data[k]
      if f_input is not None:
        f_input(t, bm.get_dt())
      ds_instance.update(t, bm.get_dt())
      return {k: v.value for k, v in included_vars.items()}

    return f_cell

  def _get_f_jocabian(self, stack=True):
    name = f'f_eval_jacobian_stack={stack}'
    if name not in self._opt_functions:
      self._opt_functions[name] = self._generate_ds_jocabian(stack)
    return self._opt_functions[name]

  def _generate_ds_jocabian(self, stack=True):
    if stack and isinstance(self.target, DynamicalSystem):
      indices = [0]
      for var in self.included_vars.values():
        indices.append(var.shape[0])
      indices = np.cumsum(indices)

      def jacob(x0):
        x0 = {k: x0[indices[i]:indices[i + 1]] for i, k in enumerate(self.included_vars.keys())}
        r = self.f_cell(x0)
        return bm.concatenate(list(r.values()))
    else:
      jacob = self.f_cell

    return bm.jit(vmap(bm.jacobian(jacob)))

  def _check_candidates(self, candidates):
    if isinstance(self.target, DynamicalSystem):
      if not isinstance(candidates, dict):
        raise ValueError(f'When "f_cell" is instance of {DynamicalSystem.__name__}, '
                         f'we should provide "candidates" as a dict, in which the key is '
                         f'the variable name with relative path, and the value '
                         f'is the candidate fixed point values. ')
      for key in candidates:
        if key not in self.included_vars:
          raise KeyError(f'"{key}" is not defined in required variables '
                         f'for fixed point optimization of {self.target}. '
                         f'Please do not provide its initial values.')

      for key in self.included_vars.keys():
        if key not in candidates:
          raise KeyError(f'"{key}" is defined in required variables '
                         f'for fixed point optimization of {self.target}. '
                         f'Please provide its initial values.')

    if isinstance(candidates, dict):
      num_candidate = np.unique([leaf.shape[0] for leaf in candidates.values()])
      if len(num_candidate) != 1:
        raise ValueError('The numbers of candidates for each variable should be the same. '
                         f'But we got {num_candidate}')
      num_candidate = num_candidate[0]
    else:
      num_candidate = candidates.shape[0]
    return num_candidate