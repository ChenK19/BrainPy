# -*- coding: utf-8 -*-


import warnings
import operator
from typing import Optional, Tuple as TupleType

import jax
import numpy as np
from jax import numpy as jnp
from jax.dtypes import canonicalize_dtype
from jax.tree_util import register_pytree_node

from brainpy.errors import MathError

__all__ = [
  'Array', 'ndarray', 'JaxArray',  # alias of Array
  'Variable',
  'TrainVar',
  'Parameter',
  'VariableView',
]

# Ways to change values in a zero-dimensional array
# -----
# Reference: https://stackoverflow.com/questions/56954714/how-do-i-assign-to-a-zero-dimensional-numpy-array
#
#   >>> x = np.array(10)
# 1. index the original array with ellipsis or an empty tuple
#    >>> x[...] = 2
#    >>> x[()] = 2

_all_slice = slice(None, None, None)

msg = ('ArrayType created outside of the jit function '
       'cannot be updated in JIT mode. You should '
       'mark it as brainpy.math.Variable instead.')

_jax_transformation_context_ = []


def add_context(name) -> None:
  _jax_transformation_context_.append(name)


def del_context(name=None) -> None:
  try:
    context = _jax_transformation_context_.pop(-1)
    if name is not None:
      if context != name:
        raise MathError('Transformation context is different!')
  except IndexError:
    raise MathError('No transformation context!')


def get_context():
  if len(_jax_transformation_context_) > 0:
    return _jax_transformation_context_[-1]
  else:
    return None


def _check_input_array(array):
  if isinstance(array, Array):
    return array.value
  elif isinstance(array, np.ndarray):
    return jnp.asarray(array)
  else:
    return array


def _return(a):
  if isinstance(a, jax.Array) and a.ndim > 0:
    return Array(a)
  return a


def _as_jax_array_(obj):
  return obj.value if isinstance(obj, Array) else obj


class Array(object):
  """Multiple-dimensional array in BrainPy.
  """

  is_brainpy_array = True
  _need_check_context = True
  __slots__ = ("_value", "_transform_context")

  def __init__(self, value, dtype=None):
    # array value
    if isinstance(value, Array):
      value = value._value
    elif isinstance(value, (tuple, list, np.ndarray)):
      value = jnp.asarray(value)
    if dtype is not None:
      value = jnp.asarray(value, dtype=dtype)
    self._value = value
    # jit mode
    self._transform_context = get_context()

  def __check_context(self) -> None:
    # raise error when in-place updating a
    if self._need_check_context:
      if self._transform_context is None:
        if len(_jax_transformation_context_) > 0:
          raise MathError(f'Array created outside of the transformation functions '
                          f'({_jax_transformation_context_[-1]}) cannot be updated. '
                          f'You should mark it as a brainpy.math.Variable instead.')
      else:
        if len(_jax_transformation_context_) > 0:
          if self._transform_context != _jax_transformation_context_[-1]:
            raise MathError(f'Array context "{self._transform_context}" differs from the JAX '
                            f'transformation context "{_jax_transformation_context_[-1]}"'
                            '\n\n'
                            'Array created in one transformation function '
                            'cannot be updated another transformation function. '
                            'You should mark it as a brainpy.math.Variable instead.')

  @property
  def value(self):
    return self._value

  @value.setter
  def value(self, value):
    self.update(value)

  def update(self, value):
    """Update the value of this Array.
    """
    if isinstance(value, Array):
      value = value.value
    elif isinstance(value, np.ndarray):
      value = jnp.asarray(value)
    elif isinstance(value, jax.Array):
      pass
    else:
      value = jnp.asarray(value)
    # check
    if value.shape != self.value.shape:
      raise MathError(f"The shape of the original data is {self.value.shape}, "
                      f"while we got {value.shape}.")
    if value.dtype != self.value.dtype:
      raise MathError(f"The dtype of the original data is {self.value.dtype}, "
                      f"while we got {value.dtype}.")
    self._value = value.value if isinstance(value, Array) else value

  @property
  def dtype(self):
    """Variable dtype."""
    return self._value.dtype

  @property
  def shape(self):
    """Variable shape."""
    return self.value.shape

  @property
  def ndim(self):
    return self.value.ndim

  @property
  def imag(self):
    return _return(self.value.image)

  @property
  def real(self):
    return _return(self.value.real)

  @property
  def size(self):
    return self.value.size

  @property
  def T(self):
    return _return(self.value.T)

  # ----------------------- #
  # Python inherent methods #
  # ----------------------- #

  def __repr__(self) -> str:
    print_code = repr(self.value)
    if ', dtype' in print_code:
      print_code = print_code.split(', dtype')[0] + ')'
    prefix = f'{self.__class__.__name__}'
    prefix2 = f'{self.__class__.__name__}(value='
    if '\n' in print_code:
      lines = print_code.split("\n")
      blank1 = " " * len(prefix2)
      lines[0] = prefix2 + lines[0]
      for i in range(1, len(lines)):
        lines[i] = blank1 + lines[i]
      lines[-1] += ","
      blank2 = " " * (len(prefix) + 1)
      lines.append(f'{blank2}dtype={self.dtype})')
      print_code = "\n".join(lines)
    else:
      print_code = prefix2 + print_code + f', dtype={self.dtype})'
    return print_code

  def __format__(self, format_spec: str) -> str:
    return format(self.value)

  def __iter__(self):
    """Solve the issue of DeviceArray.__iter__.

    Details please see JAX issues:

    - https://github.com/google/jax/issues/7713
    - https://github.com/google/jax/pull/3821
    """
    for v in self.value:
      yield v

  def __getitem__(self, index):
    if isinstance(index, slice) and (index == _all_slice):
      return self.value
    elif isinstance(index, tuple):
      index = tuple((x.value if isinstance(x, Array) else x) for x in index)
    elif isinstance(index, Array):
      index = index.value
    return self.value[index]

  def __setitem__(self, index, value):
    # value is Array
    if isinstance(value, Array):
      value = value.value
    # value is numpy.ndarray
    elif isinstance(value, np.ndarray):
      value = jnp.asarray(value)

    # index is a tuple
    if isinstance(index, tuple):
      index = tuple(_check_input_array(x) for x in index)
    # index is Array
    elif isinstance(index, Array):
      index = index.value
    # index is numpy.ndarray
    elif isinstance(index, np.ndarray):
      index = jnp.asarray(index)

    # update
    self.value = self.value.at[index].set(value)

  # ---------- #
  # operations #
  # ---------- #

  def __len__(self) -> int:
    return len(self.value)

  def __neg__(self):
    return _return(self.value.__neg__())

  def __pos__(self):
    return _return(self.value.__pos__())

  def __abs__(self):
    return _return(self.value.__abs__())

  def __invert__(self):
    return _return(self.value.__invert__())

  def __eq__(self, oc):
    return _return(self.value == _check_input_array(oc))

  def __ne__(self, oc):
    return _return(self.value != _check_input_array(oc))

  def __lt__(self, oc):
    return _return(self.value < _check_input_array(oc))

  def __le__(self, oc):
    return _return(self.value <= _check_input_array(oc))

  def __gt__(self, oc):
    return _return(self.value > _check_input_array(oc))

  def __ge__(self, oc):
    return _return(self.value >= _check_input_array(oc))

  def __add__(self, oc):
    return _return(self.value + _check_input_array(oc))

  def __radd__(self, oc):
    return _return(self.value + _check_input_array(oc))

  def __iadd__(self, oc):
    # a += b
    self.value = self.value + _check_input_array(oc)
    return self

  def __sub__(self, oc):
    return _return(self.value - _check_input_array(oc))

  def __rsub__(self, oc):
    return _return(_check_input_array(oc) - self.value)

  def __isub__(self, oc):
    # a -= b
    self.value = self.value - _check_input_array(oc)
    return self

  def __mul__(self, oc):
    return _return(self.value * _check_input_array(oc))

  def __rmul__(self, oc):
    return _return(_check_input_array(oc) * self.value)

  def __imul__(self, oc):
    # a *= b
    self.value = self.value * _check_input_array(oc)
    return self

  def __rdiv__(self, oc):
    return _return(_check_input_array(oc) / self.value)

  def __truediv__(self, oc):
    return _return(self.value / _check_input_array(oc))

  def __rtruediv__(self, oc):
    return _return(_check_input_array(oc) / self.value)

  def __itruediv__(self, oc):
    # a /= b
    self.value = self.value / _check_input_array(oc)
    return self

  def __floordiv__(self, oc):
    return _return(self.value // _check_input_array(oc))

  def __rfloordiv__(self, oc):
    return _return(_check_input_array(oc) // self.value)

  def __ifloordiv__(self, oc):
    # a //= b
    self.value = self.value // _check_input_array(oc)
    return self

  def __divmod__(self, oc):
    return _return(self.value.__divmod__(_check_input_array(oc)))

  def __rdivmod__(self, oc):
    return _return(self.value.__rdivmod__(_check_input_array(oc)))

  def __mod__(self, oc):
    return _return(self.value % _check_input_array(oc))

  def __rmod__(self, oc):
    return _return(_check_input_array(oc) % self.value)

  def __imod__(self, oc):
    # a %= b
    self.value = self.value % _check_input_array(oc)
    return self

  def __pow__(self, oc):
    return _return(self.value ** _check_input_array(oc))

  def __rpow__(self, oc):
    return _return(_check_input_array(oc) ** self.value)

  def __ipow__(self, oc):
    # a **= b
    self.value = self.value ** _check_input_array(oc)
    return self

  def __matmul__(self, oc):
    return _return(self.value @ _check_input_array(oc))

  def __rmatmul__(self, oc):
    return _return(_check_input_array(oc) @ self.value)

  def __imatmul__(self, oc):
    # a @= b
    self.value = self.value @ _check_input_array(oc)
    return self

  def __and__(self, oc):
    return _return(self.value & _check_input_array(oc))

  def __rand__(self, oc):
    return _return(_check_input_array(oc) & self.value)

  def __iand__(self, oc):
    # a &= b
    self.value = self.value & _check_input_array(oc)
    return self

  def __or__(self, oc):
    return _return(self.value | _check_input_array(oc))

  def __ror__(self, oc):
    return _return(_check_input_array(oc) | self.value)

  def __ior__(self, oc):
    # a |= b
    self.value = self.value | _check_input_array(oc)
    return self

  def __xor__(self, oc):
    return _return(self.value ^ _check_input_array(oc))

  def __rxor__(self, oc):
    return _return(_check_input_array(oc) ^ self.value)

  def __ixor__(self, oc):
    # a ^= b
    self.value = self.value ^ _check_input_array(oc)
    return self

  def __lshift__(self, oc):
    return _return(self.value << _check_input_array(oc))

  def __rlshift__(self, oc):
    return _return(_check_input_array(oc) << self.value)

  def __ilshift__(self, oc):
    # a <<= b
    self.value = self.value << _check_input_array(oc)
    return self

  def __rshift__(self, oc):
    return _return(self.value >> _check_input_array(oc))

  def __rrshift__(self, oc):
    return _return(_check_input_array(oc) >> self.value)

  def __irshift__(self, oc):
    # a >>= b
    self.value = self.value >> _check_input_array(oc)
    return self

  def __round__(self, ndigits=None):
    return _return(self.value.__round__(ndigits))

  # ----------------------- #
  #       JAX methods       #
  # ----------------------- #

  @property
  def at(self):
    return self.value.at

  def block_host_until_ready(self, *args):
    return self.value.block_host_until_ready(*args)

  def block_until_ready(self, *args):
    return self.value.block_until_ready(*args)

  def device(self):
    return self.value.device()

  @property
  def device_buffer(self):
    return self.value.device_buffer

  # ----------------------- #
  #      NumPy methods      #
  # ----------------------- #

  def all(self, axis=None, keepdims=False):
    """Returns True if all elements evaluate to True."""
    r = self.value.all(axis=axis, keepdims=keepdims)
    return _return(r)

  def any(self, axis=None, keepdims=False):
    """Returns True if any of the elements of a evaluate to True."""
    r = self.value.any(axis=axis, keepdims=keepdims)
    return _return(r)

  def argmax(self, axis=None):
    """Return indices of the maximum values along the given axis."""
    return _return(self.value.argmax(axis=axis))

  def argmin(self, axis=None):
    """Return indices of the minimum values along the given axis."""
    return _return(self.value.argmin(axis=axis))

  def argpartition(self, kth, axis=-1, kind='introselect', order=None):
    """Returns the indices that would partition this array."""
    return _return(self.value.argpartition(kth=kth, axis=axis, kind=kind, order=order))

  def argsort(self, axis=-1, kind=None, order=None):
    """Returns the indices that would sort this array."""
    return _return(self.value.argsort(axis=axis, kind=kind, order=order))

  def astype(self, dtype):
    """Copy of the array, cast to a specified type.

    Parameters
    ----------
    dtype: str, dtype
      Typecode or data-type to which the array is cast.
    """
    if dtype is None:
      return _return(self.value)
    else:
      return _return(self.value.astype(dtype))

  def byteswap(self, inplace=False):
    """Swap the bytes of the array elements

    Toggle between low-endian and big-endian data representation by
    returning a byteswapped array, optionally swapped in-place.
    Arrays of byte-strings are not swapped. The real and imaginary
    parts of a complex number are swapped individually."""
    return _return(self.value.byteswap(inplace=inplace))

  def choose(self, choices, mode='raise'):
    """Use an index array to construct a new array from a set of choices."""
    return _return(self.value.choose(choices=_as_jax_array_(choices), mode=mode))

  def clip(self, min=None, max=None):
    """Return an array whose values are limited to [min, max]. One of max or min must be given."""
    return _return(self.value.clip(min=min, max=max))

  def compress(self, condition, axis=None):
    """Return selected slices of this array along given axis."""
    return _return(self.value.compress(condition=_as_jax_array_(condition), axis=axis))

  def conj(self):
    """Complex-conjugate all elements."""
    return _return(self.value.conj())

  def conjugate(self):
    """Return the complex conjugate, element-wise."""
    return _return(self.value.conjugate())

  def copy(self):
    """Return a copy of the array."""
    return _return(self.value.copy())

  def cumprod(self, axis=None, dtype=None):
    """Return the cumulative product of the elements along the given axis."""
    return _return(self.value.cumprod(axis=axis, dtype=dtype))

  def cumsum(self, axis=None, dtype=None):
    """Return the cumulative sum of the elements along the given axis."""
    return _return(self.value.cumsum(axis=axis, dtype=dtype))

  def diagonal(self, offset=0, axis1=0, axis2=1):
    """Return specified diagonals."""
    return _return(self.value.diagonal(offset=offset, axis1=axis1, axis2=axis2))

  def dot(self, b):
    """Dot product of two arrays."""
    return _return(self.value.dot(_as_jax_array_(b)))

  def fill(self, value):
    """Fill the array with a scalar value."""
    self.value = jnp.ones_like(self.value) * value

  def flatten(self):
    return _return(self.value.flatten())

  def item(self, *args):
    """Copy an element of an array to a standard Python scalar and return it."""
    return self.value.item(*args)

  def max(self, axis=None, keepdims=False, *args, **kwargs):
    """Return the maximum along a given axis."""
    res = self.value.max(axis=axis, keepdims=keepdims, *args, **kwargs)
    return _return(res)

  def mean(self, axis=None, dtype=None, keepdims=False, *args, **kwargs):
    """Returns the average of the array elements along given axis."""
    res = self.value.mean(axis=axis, dtype=dtype, keepdims=keepdims, *args, **kwargs)
    return _return(res)

  def min(self, axis=None, keepdims=False, *args, **kwargs):
    """Return the minimum along a given axis."""
    res = self.value.min(axis=axis, keepdims=keepdims, *args, **kwargs)
    return _return(res)

  def nonzero(self):
    """Return the indices of the elements that are non-zero."""
    return tuple(_return(a) for a in self.value.nonzero())

  def prod(self, axis=None, dtype=None, keepdims=False, initial=1, where=True):
    """Return the product of the array elements over the given axis."""
    res = self.value.prod(axis=axis, dtype=dtype, keepdims=keepdims, initial=initial, where=where)
    return _return(res)

  def ptp(self, axis=None, keepdims=False):
    """Peak to peak (maximum - minimum) value along a given axis."""
    r = self.value.ptp(axis=axis, keepdims=keepdims)
    return _return(r)

  def put(self, indices, values):
    """Replaces specified elements of an array with given values.

    Parameters
    ----------
    indices: array_like
      Target indices, interpreted as integers.
    values: array_like
      Values to place in the array at target indices.
    """
    self.__setitem__(indices, values)

  def ravel(self, order=None):
    """Return a flattened array."""
    return _return(self.value.ravel(order=order))

  def repeat(self, repeats, axis=None):
    """Repeat elements of an array."""
    return _return(self.value.repeat(repeats=repeats, axis=axis))

  def reshape(self, *shape, order='C'):
    """Returns an array containing the same data with a new shape."""
    return _return(self.value.reshape(*shape, order=order))

  def resize(self, new_shape):
    """Change shape and size of array in-place."""
    self.value = self.value.reshape(new_shape)

  def round(self, decimals=0):
    """Return ``a`` with each element rounded to the given number of decimals."""
    return _return(self.value.round(decimals=decimals))

  def searchsorted(self, v, side='left', sorter=None):
    """Find indices where elements should be inserted to maintain order.

    Find the indices into a sorted array `a` such that, if the
    corresponding elements in `v` were inserted before the indices, the
    order of `a` would be preserved.

    Assuming that `a` is sorted:

    ======  ============================
    `side`  returned index `i` satisfies
    ======  ============================
    left    ``a[i-1] < v <= a[i]``
    right   ``a[i-1] <= v < a[i]``
    ======  ============================

    Parameters
    ----------
    v : array_like
        Values to insert into `a`.
    side : {'left', 'right'}, optional
        If 'left', the index of the first suitable location found is given.
        If 'right', return the last such index.  If there is no suitable
        index, return either 0 or N (where N is the length of `a`).
    sorter : 1-D array_like, optional
        Optional array of integer indices that sort array a into ascending
        order. They are typically the result of argsort.

    Returns
    -------
    indices : array of ints
        Array of insertion points with the same shape as `v`.
    """
    return _return(self.value.searchsorted(v=_as_jax_array_(v), side=side, sorter=sorter))

  def sort(self, axis=-1, kind='quicksort', order=None):
    """Sort an array in-place.

    Parameters
    ----------
    axis : int, optional
        Axis along which to sort. Default is -1, which means sort along the
        last axis.
    kind : {'quicksort', 'mergesort', 'heapsort', 'stable'}
        Sorting algorithm. The default is 'quicksort'. Note that both 'stable'
        and 'mergesort' use timsort under the covers and, in general, the
        actual implementation will vary with datatype. The 'mergesort' option
        is retained for backwards compatibility.
    order : str or list of str, optional
        When `a` is an array with fields defined, this argument specifies
        which fields to compare first, second, etc.  A single field can
        be specified as a string, and not all fields need be specified,
        but unspecified fields will still be used, in the order in which
        they come up in the dtype, to break ties.
    """
    self.value = self.value.sort(axis=axis, kind=kind, order=order)

  def squeeze(self, axis=None):
    """Remove axes of length one from ``a``."""
    return _return(self.value.squeeze(axis=axis))

  def std(self, axis=None, dtype=None, ddof=0, keepdims=False):
    """Compute the standard deviation along the specified axis.

    Returns the standard deviation, a measure of the spread of a distribution,
    of the array elements. The standard deviation is computed for the
    flattened array by default, otherwise over the specified axis.

    Parameters
    ----------
    axis : None or int or tuple of ints, optional
        Axis or axes along which the standard deviation is computed. The
        default is to compute the standard deviation of the flattened array.
        If this is a tuple of ints, a standard deviation is performed over
        multiple axes, instead of a single axis or all the axes as before.
    dtype : dtype, optional
        Type to use in computing the standard deviation. For arrays of
        integer type the default is float64, for arrays of float types it is
        the same as the array type.
    ddof : int, optional
        Means Delta Degrees of Freedom.  The divisor used in calculations
        is ``N - ddof``, where ``N`` represents the number of elements.
        By default `ddof` is zero.
    keepdims : bool, optional
        If this is set to True, the axes which are reduced are left
        in the result as dimensions with size one. With this option,
        the result will broadcast correctly against the input array.

        If the default value is passed, then `keepdims` will not be
        passed through to the `std` method of sub-classes of
        `ndarray`, however any non-default value will be.  If the
        sub-class' method does not implement `keepdims` any
        exceptions will be raised.

    Returns
    -------
    standard_deviation : ndarray, see dtype parameter above.
        If `out` is None, return a new array containing the standard deviation,
        otherwise return a reference to the output array.
    """
    r = self.value.std(axis=axis, dtype=dtype, ddof=ddof, keepdims=keepdims)
    return _return(r)

  def sum(self, axis=None, dtype=None, keepdims=False, initial=0, where=True):
    """Return the sum of the array elements over the given axis."""
    res = self.value.sum(axis=axis, dtype=dtype, keepdims=keepdims, initial=initial, where=where)
    return _return(res)

  def swapaxes(self, axis1, axis2):
    """Return a view of the array with `axis1` and `axis2` interchanged."""
    return _return(self.value.swapaxes(axis1, axis2))

  def split(self, indices_or_sections, axis=0):
    """Split an array into multiple sub-arrays as views into ``ary``.

    Parameters
    ----------
    indices_or_sections : int, 1-D array
      If `indices_or_sections` is an integer, N, the array will be divided
      into N equal arrays along `axis`.  If such a split is not possible,
      an error is raised.

      If `indices_or_sections` is a 1-D array of sorted integers, the entries
      indicate where along `axis` the array is split.  For example,
      ``[2, 3]`` would, for ``axis=0``, result in

        - ary[:2]
        - ary[2:3]
        - ary[3:]

      If an index exceeds the dimension of the array along `axis`,
      an empty sub-array is returned correspondingly.
    axis : int, optional
      The axis along which to split, default is 0.

    Returns
    -------
    sub-arrays : list of ndarrays
      A list of sub-arrays as views into `ary`.
    """
    return [_return(a) for a in self.value.split(indices_or_sections, axis=axis)]

  def take(self, indices, axis=None, mode=None):
    """Return an array formed from the elements of a at the given indices."""
    return _return(self.value.take(indices=_as_jax_array_(indices), axis=axis, mode=mode))

  def tobytes(self):
    """Construct Python bytes containing the raw data bytes in the array.

    Constructs Python bytes showing a copy of the raw contents of data memory.
    The bytes object is produced in C-order by default. This behavior is
    controlled by the ``order`` parameter."""
    return self.value.tobytes()

  def tolist(self):
    """Return the array as an ``a.ndim``-levels deep nested list of Python scalars.

    Return a copy of the array data as a (nested) Python list.
    Data items are converted to the nearest compatible builtin Python type, via
    the `~numpy.ndarray.item` function.

    If ``a.ndim`` is 0, then since the depth of the nested list is 0, it will
    not be a list at all, but a simple Python scalar.
    """
    return self.value.tolist()

  def trace(self, offset=0, axis1=0, axis2=1, dtype=None):
    """Return the sum along diagonals of the array."""
    return _return(self.value.trace(offset=offset, axis1=axis1, axis2=axis2, dtype=dtype))

  def transpose(self, *axes):
    """Returns a view of the array with axes transposed.

    For a 1-D array this has no effect, as a transposed vector is simply the
    same vector. To convert a 1-D array into a 2D column vector, an additional
    dimension must be added. `np.atleast2d(a).T` achieves this, as does
    `a[:, np.newaxis]`.
    For a 2-D array, this is a standard matrix transpose.
    For an n-D array, if axes are given, their order indicates how the
    axes are permuted (see Examples). If axes are not provided and
    ``a.shape = (i[0], i[1], ... i[n-2], i[n-1])``, then
    ``a.transpose().shape = (i[n-1], i[n-2], ... i[1], i[0])``.

    Parameters
    ----------
    axes : None, tuple of ints, or `n` ints

     * None or no argument: reverses the order of the axes.

     * tuple of ints: `i` in the `j`-th place in the tuple means `a`'s
       `i`-th axis becomes `a.transpose()`'s `j`-th axis.

     * `n` ints: same as an n-tuple of the same ints (this form is
       intended simply as a "convenience" alternative to the tuple form)

    Returns
    -------
    out : ndarray
        View of `a`, with axes suitably permuted.
    """
    return _return(self.value.transpose(*axes))

  def tile(self, reps):
    """Construct an array by repeating A the number of times given by reps.

    If `reps` has length ``d``, the result will have dimension of
    ``max(d, A.ndim)``.

    If ``A.ndim < d``, `A` is promoted to be d-dimensional by prepending new
    axes. So a shape (3,) array is promoted to (1, 3) for 2-D replication,
    or shape (1, 1, 3) for 3-D replication. If this is not the desired
    behavior, promote `A` to d-dimensions manually before calling this
    function.

    If ``A.ndim > d``, `reps` is promoted to `A`.ndim by pre-pending 1's to it.
    Thus for an `A` of shape (2, 3, 4, 5), a `reps` of (2, 2) is treated as
    (1, 1, 2, 2).

    Note : Although tile may be used for broadcasting, it is strongly
    recommended to use numpy's broadcasting operations and functions.

    Parameters
    ----------
    reps : array_like
        The number of repetitions of `A` along each axis.

    Returns
    -------
    c : ndarray
        The tiled output array.
    """
    return _return(self.value.tile(_as_jax_array_(reps)))

  def var(self, axis=None, dtype=None, ddof=0, keepdims=False):
    """Returns the variance of the array elements, along given axis."""
    r = self.value.var(axis=axis, dtype=dtype, ddof=ddof, keepdims=keepdims)
    return _return(r)

  def view(self, *args, dtype=None):
    r"""New view of array with the same data.

    This function is compatible with pytorch syntax.

    Returns a new tensor with the same data as the :attr:`self` tensor but of a
    different :attr:`shape`.

    The returned tensor shares the same data and must have the same number
    of elements, but may have a different size. For a tensor to be viewed, the new
    view size must be compatible with its original size and stride, i.e., each new
    view dimension must either be a subspace of an original dimension, or only span
    across original dimensions :math:`d, d+1, \dots, d+k` that satisfy the following
    contiguity-like condition that :math:`\forall i = d, \dots, d+k-1`,

    .. math::

      \text{stride}[i] = \text{stride}[i+1] \times \text{size}[i+1]

    Otherwise, it will not be possible to view :attr:`self` tensor as :attr:`shape`
    without copying it (e.g., via :meth:`contiguous`). When it is unclear whether a
    :meth:`view` can be performed, it is advisable to use :meth:`reshape`, which
    returns a view if the shapes are compatible, and copies (equivalent to calling
    :meth:`contiguous`) otherwise.

    Args:
        shape (int...): the desired size

    Example::

        >>> x = brainpy.math.randn(4, 4)
        >>> x.size
       [4, 4]
        >>> y = x.view(16)
        >>> y.size
        [16]
        >>> z = x.view(-1, 8)  # the size -1 is inferred from other dimensions
        >>> z.size
        [2, 8]

        >>> a = brainpy.math.randn(1, 2, 3, 4)
        >>> a.size
        [1, 2, 3, 4]
        >>> b = a.transpose(1, 2)  # Swaps 2nd and 3rd dimension
        >>> b.size
        [1, 3, 2, 4]
        >>> c = a.view(1, 3, 2, 4)  # Does not change tensor layout in memory
        >>> c.size
        [1, 3, 2, 4]
        >>> brainpy.math.equal(b, c)
        False


    .. method:: view(dtype) -> Tensor
       :noindex:

    Returns a new tensor with the same data as the :attr:`self` tensor but of a
    different :attr:`dtype`.

    If the element size of :attr:`dtype` is different than that of ``self.dtype``,
    then the size of the last dimension of the output will be scaled
    proportionally.  For instance, if :attr:`dtype` element size is twice that of
    ``self.dtype``, then each pair of elements in the last dimension of
    :attr:`self` will be combined, and the size of the last dimension of the output
    will be half that of :attr:`self`. If :attr:`dtype` element size is half that
    of ``self.dtype``, then each element in the last dimension of :attr:`self` will
    be split in two, and the size of the last dimension of the output will be
    double that of :attr:`self`. For this to be possible, the following conditions
    must be true:

        * ``self.dim()`` must be greater than 0.
        * ``self.stride(-1)`` must be 1.

    Additionally, if the element size of :attr:`dtype` is greater than that of
    ``self.dtype``, the following conditions must be true as well:

        * ``self.size(-1)`` must be divisible by the ratio between the element
          sizes of the dtypes.
        * ``self.storage_offset()`` must be divisible by the ratio between the
          element sizes of the dtypes.
        * The strides of all dimensions, except the last dimension, must be
          divisible by the ratio between the element sizes of the dtypes.

    If any of the above conditions are not met, an error is thrown.


    Args:
        dtype (:class:`dtype`): the desired dtype

    Example::

        >>> x = brainpy.math.randn(4, 4)
        >>> x
        Array([[ 0.9482, -0.0310,  1.4999, -0.5316],
                [-0.1520,  0.7472,  0.5617, -0.8649],
                [-2.4724, -0.0334, -0.2976, -0.8499],
                [-0.2109,  1.9913, -0.9607, -0.6123]])
        >>> x.dtype
        brainpy.math.float32

        >>> y = x.view(brainpy.math.int32)
        >>> y
        tensor([[ 1064483442, -1124191867,  1069546515, -1089989247],
                [-1105482831,  1061112040,  1057999968, -1084397505],
                [-1071760287, -1123489973, -1097310419, -1084649136],
                [-1101533110,  1073668768, -1082790149, -1088634448]],
            dtype=brainpy.math.int32)
        >>> y[0, 0] = 1000000000
        >>> x
        tensor([[ 0.0047, -0.0310,  1.4999, -0.5316],
                [-0.1520,  0.7472,  0.5617, -0.8649],
                [-2.4724, -0.0334, -0.2976, -0.8499],
                [-0.2109,  1.9913, -0.9607, -0.6123]])

        >>> x.view(brainpy.math.cfloat)
        tensor([[ 0.0047-0.0310j,  1.4999-0.5316j],
                [-0.1520+0.7472j,  0.5617-0.8649j],
                [-2.4724-0.0334j, -0.2976-0.8499j],
                [-0.2109+1.9913j, -0.9607-0.6123j]])
        >>> x.view(brainpy.math.cfloat).size
        [4, 2]

        >>> x.view(brainpy.math.uint8)
        tensor([[  0, 202, 154,  59, 182, 243, 253, 188, 185, 252, 191,  63, 240,  22,
                   8, 191],
                [227, 165,  27, 190, 128,  72,  63,  63, 146, 203,  15,  63,  22, 106,
                  93, 191],
                [205,  59,  30, 192, 112, 206,   8, 189,   7,  95, 152, 190,  12, 147,
                  89, 191],
                [ 43, 246,  87, 190, 235, 226, 254,  63, 111, 240, 117, 191, 177, 191,
                  28, 191]], dtype=brainpy.math.uint8)
        >>> x.view(brainpy.math.uint8).size
        [4, 16]

    """
    if len(args) == 0:
      if dtype is None:
        raise ValueError('Provide dtype or shape.')
      else:
        return _return(self.value.view(dtype))
    else:
      if isinstance(args[0], int): # shape
        if dtype is not None:
          raise ValueError('Provide one of dtype or shape. Not both.')
        return _return(self.value.reshape(*args))
      else: # dtype
        assert not isinstance(args[0], int)
        assert dtype is None
        return _return(self.value.view(args[0]))

  # ------------------
  # NumPy support
  # ------------------

  def numpy(self, dtype=None):
    """Convert to numpy.ndarray."""
    # warnings.warn('Deprecated since 2.1.12. Please use ".to_numpy()" instead.', DeprecationWarning)
    return np.asarray(self.value, dtype=dtype)

  def to_numpy(self, dtype=None):
    """Convert to numpy.ndarray."""
    return np.asarray(self.value, dtype=dtype)

  def to_jax(self, dtype=None):
    """Convert to jax.numpy.ndarray."""
    if dtype is None:
      return self.value
    else:
      return jnp.asarray(self.value, dtype=dtype)

  def __array__(self, dtype=None):
    """Support ``numpy.array()`` and ``numpy.asarray()`` functions."""
    return np.asarray(self.value, dtype=dtype)

  def __jax_array__(self):
    return self.value

  def as_variable(self):
    """As an instance of Variable."""
    return Variable(self)

  def __format__(self, specification):
    return self.value.__format__(specification)

  def __bool__(self) -> bool:
    return self.value.__bool__()

  def __float__(self):
    return self.value.__float__()

  def __int__(self):
    return self.value.__int__()

  def __complex__(self):
    return self.value.__complex__()

  def __hex__(self):
    assert self.ndim == 0, 'hex only works on scalar values'
    return hex(self.value)  # type: ignore

  def __oct__(self):
    assert self.ndim == 0, 'oct only works on scalar values'
    return oct(self.value)  # type: ignore

  def __index__(self):
    return operator.index(self.value)

  def __dlpack__(self):
    from jax.dlpack import to_dlpack  # pylint: disable=g-import-not-at-top
    return to_dlpack(self.value)


JaxArray = Array
ndarray = Array


class Variable(Array):
  """The pointer to specify the dynamical variable.

  Initializing an instance of ``Variable`` by two ways:

  >>> import brainpy.math as bm
  >>> # 1. init a Variable by the concreate data
  >>> v1 = bm.Variable(bm.zeros(10))
  >>> # 2. init a Variable by the data shape
  >>> v2 = bm.Variable(10)

  Note that when initializing a `Variable` by the data shape,
  all values in this `Variable` will be initialized as zeros.

  Parameters
  ----------
  value_or_size: Shape, Array, int
    The value or the size of the value.
  dtype:
    The type of the data.
  batch_axis: optional, int
    The batch axis.
  """

  _need_check_context = False
  __slots__ = ('_value', '_batch_axis')

  def __init__(
      self,
      value_or_size,
      dtype: type = None,
      batch_axis: int = None,
  ):
    if isinstance(value_or_size, int):
      value = jnp.zeros(value_or_size, dtype=dtype)
    elif isinstance(value_or_size, (tuple, list)) and all([isinstance(s, int) for s in value_or_size]):
      value = jnp.zeros(value_or_size, dtype=dtype)
    else:
      value = value_or_size

    super(Variable, self).__init__(value, dtype=dtype)

    # check batch axis
    if isinstance(value, Variable):
      if value.batch_axis is not None and batch_axis is not None:
        if batch_axis != value.batch_axis:
          raise ValueError(f'"batch_axis" is not consistent. Got batch_axis in the given value '
                           f'is {value.batch_axis}, but the specified batch_axis is {batch_axis}')
      batch_axis = value.batch_axis

    # assign batch axis
    self._batch_axis = batch_axis
    if batch_axis is not None:
      if batch_axis >= self.ndim:
        raise MathError(f'This variables has {self.ndim} dimension, '
                        f'but the batch axis is set to be {batch_axis}.')

  @property
  def nobatch_shape(self) -> TupleType[int, ...]:
    """Shape without batch axis."""
    if self.batch_axis is not None:
      shape = list(self.value.shape)
      shape.pop(self.batch_axis)
      return tuple(shape)
    else:
      return self.shape

  @property
  def batch_axis(self) -> Optional[int]:
    return self._batch_axis

  @batch_axis.setter
  def batch_axis(self, val):
    raise ValueError(f'Cannot set "batch_axis" after creating a {self.__class__.__name__} instance.')

  @property
  def batch_size(self) -> Optional[int]:
    if self.batch_axis is None:
      return None
    else:
      return self.shape[self.batch_axis]

  @batch_size.setter
  def batch_size(self, val):
    raise ValueError(f'Cannot set "batch_size" manually.')

  def update(self, value):
    """Update the value of this Array.
    """
    if self._batch_axis is None:
      ext_shape = jnp.shape(value)
      int_shape = self.shape
    else:
      ext_shape = value.shape[:self._batch_axis] + value.shape[self._batch_axis + 1:]
      int_shape = self.shape[:self._batch_axis] + self.shape[self._batch_axis + 1:]
    if ext_shape != int_shape:
      error = f"The shape of the original data is {self.shape}, while we got {value.shape}"
      error += f' with batch_axis={self._batch_axis}.'
      raise MathError(error)
    if hasattr(value, 'dtype'):
      dtype = value.dtype
    else:
      dtype = canonicalize_dtype(type(value))
    if dtype != self.dtype:
      raise MathError(f"The dtype of the original data is {self.dtype}, "
                      f"while we got {dtype}.")
    self._value = value.value if isinstance(value, Array) else value


class TrainVar(Variable):
  """The pointer to specify the trainable variable.
  """
  __slots__ = ('_value', '_batch_axis')

  def __init__(self,
               value_or_size,
               dtype: type = None,
               batch_axis: int = None):
    super(TrainVar, self).__init__(value_or_size,
                                   dtype=dtype,
                                   batch_axis=batch_axis)


class Parameter(Variable):
  """The pointer to specify the parameter.
  """
  __slots__ = ('_value', '_batch_axis')

  def __init__(self,
               value_or_size,
               dtype: type = None,
               batch_axis: int = None):
    super(Parameter, self).__init__(value_or_size,
                                    dtype=dtype,
                                    batch_axis=batch_axis)


class ParallelVariable(Variable):
  pass


class BatchVariable(Variable):
  pass


class VariableView(Variable):
  """A view of a Variable instance.

  This class is used to create a subset view of ``brainpy.math.Variable``.

  >>> import brainpy.math as bm
  >>> bm.random.seed(123)
  >>> origin = bm.Variable(bm.random.random(5))
  >>> view = bm.VariableView(origin, slice(None, 2, None))  # origin[:2]
  VariableView([0.02920651, 0.19066381], dtype=float32)

  ``VariableView`` can be used to update the subset of the original
  Variable instance, and make operations on this subset of the Variable.

  >>> view[:] = 1.
  >>> view
  VariableView([1., 1.], dtype=float32)
  >>> origin
  Variable([1.       , 1.       , 0.5482849, 0.6564884, 0.8446237], dtype=float32)
  >>> view + 10
  Array([11., 11.], dtype=float32)
  >>> view *= 10
  VariableView([10., 10.], dtype=float32)

  The above example demonstrates that the updating of an ``VariableView`` instance
  is actually made in the original ``Variable`` instance.

  Moreover, it's worthy to note that ``VariableView`` is not a PyTree.
  """

  def __init__(self, value: Variable, index):
    self.index = jax.tree_util.tree_map(_as_jax_array_, index, is_leaf=lambda a: isinstance(a, Array))
    if not isinstance(value, Variable):
      raise ValueError('Must be instance of Variable.')
    super(VariableView, self).__init__(value.value, batch_axis=value.batch_axis)
    self._value = value

  def __repr__(self) -> str:
    print_code = repr(self._value)
    prefix = f'{self.__class__.__name__}'
    blank = " " * (len(prefix) + 1)
    lines = print_code.split("\n")
    lines[0] = prefix + "(" + lines[0]
    for i in range(1, len(lines)):
      lines[i] = blank + lines[i]
    lines[-1] += ","
    lines.append(blank + f'index={self.index})')
    print_code = "\n".join(lines)
    return print_code

  @property
  def value(self):
    return self._value[self.index]

  @value.setter
  def value(self, v):
    self.update(v)

  def update(self, value):
    int_shape = self.shape
    if self.batch_axis is None:
      ext_shape = value.shape
    else:
      ext_shape = value.shape[:self.batch_axis] + value.shape[self.batch_axis + 1:]
      int_shape = int_shape[:self.batch_axis] + int_shape[self.batch_axis + 1:]
    if ext_shape != int_shape:
      error = f"The shape of the original data is {self.shape}, while we got {value.shape}"
      if self.batch_axis is None:
        error += '. Do you forget to set "batch_axis" when initialize this variable?'
      else:
        error += f' with batch_axis={self.batch_axis}.'
      raise MathError(error)
    if value.dtype != self._value.dtype:
      raise MathError(f"The dtype of the original data is {self._value.dtype}, "
                      f"while we got {value.dtype}.")
    self._value[self.index] = value.value if isinstance(value, Array) else value


def _jaxarray_unflatten(aux_data, flat_contents):
  r = Array(*flat_contents)
  r._transform_context = aux_data[0]
  return r


register_pytree_node(Array,
                     lambda t: ((t.value,), (t._transform_context,)),
                     _jaxarray_unflatten)

register_pytree_node(Variable,
                     lambda t: ((t.value,), None),
                     lambda aux_data, flat_contents: Variable(*flat_contents))

register_pytree_node(TrainVar,
                     lambda t: ((t.value,), None),
                     lambda aux_data, flat_contents: TrainVar(*flat_contents))

register_pytree_node(Parameter,
                     lambda t: ((t.value,), None),
                     lambda aux_data, flat_contents: Parameter(*flat_contents))
