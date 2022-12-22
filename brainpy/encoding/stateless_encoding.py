# -*- coding: utf-8 -*-

from typing import Union

import brainpy.math as bm
from brainpy.tools import checking
from brainpy.types import ArrayType
from .base import Encoder

__all__ = [
  'PoissonEncoder',
]


class PoissonEncoder(Encoder):
  r"""Encode the rate input as the Poisson spike train.

  Given the input :math:`x`, the poisson encoder will output
  spikes whose firing probability is :math:`x_{\text{normalize}}`, where
  :math:`x_{\text{normalize}}` is normalized into ``[0, 1]`` according
  to :math:`x_{\text{normalize}} = \frac{x-\text{min_val}}{\text{max_val} - \text{min_val}}`.

  Parameters
  ----------
  min_val: float
    The minimal value in the given data `x`, used to the data normalization.
  max_val: float
    The maximum value in the given data `x`, used to the data normalization.
  seed: int, ArrayType
    The seed or key for random generation.
  """

  def __init__(self,
               min_val: float,
               max_val: float,
               seed: Union[int, ArrayType] = None):
    super().__init__()

    checking.check_float(min_val, 'min_val')
    checking.check_float(max_val, 'max_val')
    self.min_val = min_val
    self.max_val = max_val
    self.rng = bm.random.RandomState(seed)

  def __call__(self, x: ArrayType, num_time: int = None):
    """

    Parameters
    ----------
    x: ArrayType
      The rate input.
    num_time: int
      Encode rate values as spike trains in the given time length.

      - If ``time_len=None``, encode the rate values at the current time step.
        Users should repeatedly call it to encode `x` as a spike train.
      - Else, given the ``x`` with shape ``(S, ...)``, the encoded
        spike train is the array with shape ``(time_len, S, ...)``.

    Returns
    -------
    out: ArrayType
      The encoded spike train.
    """
    checking.check_integer(num_time, 'time_len', min_bound=1, allow_none=True)
    x = (x - self.min_val) / (self.max_val - self.min_val)
    shape = x.shape if (num_time is None) else ((num_time,) + x.shape)
    d = self.rng.rand(*shape).value < x
    return d.astype(x.dtype)
