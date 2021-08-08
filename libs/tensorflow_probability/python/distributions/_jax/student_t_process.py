# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""The StudentTProcess distribution class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import warnings

# Dependency imports
from tensorflow_probability.python.internal.backend.jax.compat import v2 as tf

from tensorflow_probability.python.bijectors._jax import identity as identity_bijector
from tensorflow_probability.python.distributions._jax import distribution
from tensorflow_probability.python.distributions._jax import multivariate_student_t
from tensorflow_probability.python.distributions._jax import student_t
from tensorflow_probability.python.internal._jax import assert_util
from tensorflow_probability.python.internal._jax import dtype_util
from tensorflow_probability.python.internal import reparameterization
from tensorflow_probability.python.internal._jax import tensor_util
from tensorflow_probability.python.internal._jax import tensorshape_util

__all__ = [
    'StudentTProcess',
]


def _add_diagonal_shift(matrix, shift):
  return tf.linalg.set_diag(
      matrix, tf.linalg.diag_part(matrix) + shift, name='add_diagonal_shift')


class StudentTProcess(distribution.Distribution):
  """Marginal distribution of a Student's T process at finitely many points.

  A Student's T process (TP) is an indexed collection of random variables, any
  finite collection of which are jointly Multivariate Student's T. While this
  definition applies to finite index sets, it is typically implicit that the
  index set is infinite; in applications, it is often some finite dimensional
  real or complex vector space. In such cases, the TP may be thought of as a
  distribution over (real- or complex-valued) functions defined over the index
  set.

  Just as Student's T distributions are fully specified by their degrees of
  freedom, location and scale, a Student's T process can be completely specified
  by a degrees of freedom parameter, mean function and covariance function.
  Let `S` denote the index set and `K` the space in
  which each indexed random variable takes its values (again, often R or C).
  The mean function is then a map `m: S -> K`, and the covariance function,
  or kernel, is a positive-definite function `k: (S x S) -> K`. The properties
  of functions drawn from a TP are entirely dictated (up to translation) by
  the form of the kernel function.

  This `Distribution` represents the marginal joint distribution over function
  values at a given finite collection of points `[x[1], ..., x[N]]` from the
  index set `S`. By definition, this marginal distribution is just a
  multivariate Student's T distribution, whose mean is given by the vector
  `[ m(x[1]), ..., m(x[N]) ]` and whose covariance matrix is constructed from
  pairwise applications of the kernel function to the given inputs:

  ```none
      | k(x[1], x[1])    k(x[1], x[2])  ...  k(x[1], x[N]) |
      | k(x[2], x[1])    k(x[2], x[2])  ...  k(x[2], x[N]) |
      |      ...              ...                 ...      |
      | k(x[N], x[1])    k(x[N], x[2])  ...  k(x[N], x[N]) |
  ```

  For this to be a valid covariance matrix, it must be symmetric and positive
  definite; hence the requirement that `k` be a positive definite function
  (which, by definition, says that the above procedure will yield PD matrices).

  Note also we use a parameterization as suggested in [1], which requires `df`
  to be greater than 2. This allows for the covariance for any finite
  dimensional marginal of the TP (a multivariate Student's T distribution) to
  just be the PD matrix generated by the kernel.


  #### Mathematical Details

  The probability density function (pdf) is a multivariate Student's T whose
  parameters are derived from the TP's properties:

  ```none
  pdf(x; df, index_points, mean_fn, kernel) = MultivariateStudentT(df, loc, K)
  K = (df - 2) / df  * (kernel.matrix(index_points, index_points) +
       jitter * eye(N))
  loc = (x - mean_fn(index_points))^T @ K @ (x - mean_fn(index_points))
  ```

  where:

  * `df` is the degrees of freedom parameter for the TP.
  * `index_points` are points in the index set over which the TP is defined,
  * `mean_fn` is a callable mapping the index set to the TP's mean values,
  * `kernel` is `PositiveSemidefiniteKernel`-like and represents the covariance
    function of the TP,
  * `jitter` is added to the diagonal to ensure positive definiteness up to
     machine precision (otherwise Cholesky-decomposition is prone to failure),
  * `eye(N)` is an N-by-N identity matrix.

  #### Examples

  ##### Draw joint samples from a TP prior

  ```python
  import numpy as np
  from tensorflow_probability.python.internal.backend.jax.compat import v2 as tf
  import tensorflow_probability as tfp; tfp = tfp.experimental.substrates.jax

  tf.enable_v2_behavior()

  tfd = tfp.distributions
  psd_kernels = tfp.math.psd_kernels

  num_points = 100
  # Index points should be a collection (100, here) of feature vectors. In this
  # example, we're using 1-d vectors, so we just need to reshape the output from
  # np.linspace, to give a shape of (100, 1).
  index_points = np.expand_dims(np.linspace(-1., 1., num_points), -1)

  # Define a kernel with default parameters.
  kernel = psd_kernels.ExponentiatedQuadratic()

  tp = tfd.StudentTProcess(3., kernel, index_points)

  samples = tp.sample(10)
  # ==> 10 independently drawn, joint samples at `index_points`

  noisy_tp = tfd.StudentTProcess(
      df=3.,
      kernel=kernel,
      index_points=index_points)
  noisy_samples = noisy_tp.sample(10)
  # ==> 10 independently drawn, noisy joint samples at `index_points`
  ```

  ##### Optimize kernel parameters via maximum marginal likelihood.

  ```python
  # Suppose we have some data from a known function. Note the index points in
  # general have shape `[b1, ..., bB, f1, ..., fF]` (here we assume `F == 1`),
  # so we need to explicitly consume the feature dimensions (just the last one
  # here).
  f = lambda x: np.sin(10*x[..., 0]) * np.exp(-x[..., 0]**2)
  observed_index_points = np.expand_dims(np.random.uniform(-1., 1., 50), -1)
  # Squeeze to take the shape from [50, 1] to [50].
  observed_values = f(observed_index_points)

  amplitude = tfp.util.TransformedVariable(
      1., tfp.bijectors.Softplus(), dtype=np.float64, name='amplitude')
  length_scale = tfp.util.TransformedVariable(
      1., tfp.bijectors.Softplus(), dtype=np.float64, name='length_scale')

  # Define a kernel with trainable parameters.
  kernel = psd_kernels.ExponentiatedQuadratic(
      amplitude=amplitude,
      length_scale=length_scale)

  tp = tfd.StudentTProcess(3., kernel, observed_index_points)

  optimizer = tf.optimizers.Adam()

  @tf.function
  def optimize():
    with tf.GradientTape() as tape:
      loss = -tp.log_prob(observed_values)
    grads = tape.gradient(loss, tp.trainable_variables)
    optimizer.apply_gradients(zip(grads, tp.trainable_variables))
    return loss

  for i in range(1000):
    nll = optimize()
    if i % 100 == 0:
      print("Step {}: NLL = {}".format(i, nll))
  print("Final NLL = {}".format(nll))
  ```

  #### References

  [1]: Amar Shah, Andrew Gordon Wilson, and Zoubin Ghahramani. Student-t
       Processes as Alternatives to Gaussian Processes. In _Artificial
       Intelligence and Statistics_, 2014.
       https://www.cs.cmu.edu/~andrewgw/tprocess.pdf
  """

  def __init__(self,
               df,
               kernel,
               index_points=None,
               mean_fn=None,
               jitter=1e-6,
               validate_args=False,
               allow_nan_stats=False,
               name='StudentTProcess'):
    """Instantiate a StudentTProcess Distribution.

    Args:
      df: Positive Floating-point `Tensor` representing the degrees of freedom.
        Must be greater than 2.
      kernel: `PositiveSemidefiniteKernel`-like instance representing the
        TP's covariance function.
      index_points: `float` `Tensor` representing finite (batch of) vector(s) of
        points in the index set over which the TP is defined. Shape has the form
        `[b1, ..., bB, e, f1, ..., fF]` where `F` is the number of feature
        dimensions and must equal `kernel.feature_ndims` and `e` is the number
        (size) of index points in each batch. Ultimately this distribution
        corresponds to a `e`-dimensional multivariate Student's T. The batch
        shape must be broadcastable with `kernel.batch_shape` and any batch dims
        yielded by `mean_fn`.
      mean_fn: Python `callable` that acts on `index_points` to produce a (batch
        of) vector(s) of mean values at `index_points`. Takes a `Tensor` of
        shape `[b1, ..., bB, f1, ..., fF]` and returns a `Tensor` whose shape is
        broadcastable with `[b1, ..., bB]`. Default value: `None` implies
        constant zero function.
      jitter: `float` scalar `Tensor` added to the diagonal of the covariance
        matrix to ensure positive definiteness of the covariance matrix.
        Default value: `1e-6`.
      validate_args: Python `bool`, default `False`. When `True` distribution
        parameters are checked for validity despite possibly degrading runtime
        performance. When `False` invalid inputs may silently render incorrect
        outputs.
        Default value: `False`.
      allow_nan_stats: Python `bool`, default `True`. When `True`,
        statistics (e.g., mean, mode, variance) use the value "`NaN`" to
        indicate the result is undefined. When `False`, an exception is raised
        if one or more of the statistic's batch members are undefined.
        Default value: `False`.
      name: Python `str` name prefixed to Ops created by this class.
        Default value: "StudentTProcess".

    Raises:
      ValueError: if `mean_fn` is not `None` and is not callable.
    """
    parameters = dict(locals())
    with tf.name_scope(name) as name:
      dtype = dtype_util.common_dtype(
          [df, index_points, jitter], tf.float32)
      df = tensor_util.convert_nonref_to_tensor(df, dtype=dtype, name='df')
      index_points = tensor_util.convert_nonref_to_tensor(
          index_points, dtype=dtype, name='index_points')
      jitter = tensor_util.convert_nonref_to_tensor(
          jitter, dtype=dtype, name='jitter')

      self._kernel = kernel
      self._index_points = index_points
      # Default to a constant zero function, borrowing the dtype from
      # index_points to ensure consistency.
      if mean_fn is None:
        mean_fn = lambda x: tf.zeros([1], dtype=dtype)
      else:
        if not callable(mean_fn):
          raise ValueError('`mean_fn` must be a Python callable')
      self._df = df
      self._mean_fn = mean_fn
      self._jitter = jitter

      with tf.name_scope('init'):
        super(StudentTProcess, self).__init__(
            dtype=dtype,
            reparameterization_type=reparameterization.FULLY_REPARAMETERIZED,
            validate_args=validate_args,
            allow_nan_stats=allow_nan_stats,
            parameters=parameters,
            name=name)

  def _is_univariate_marginal(self, index_points):
    """True if the given index_points would yield a univariate marginal.

    Args:
      index_points: the set of index set locations at which to compute the
      marginal Student T distribution. If this set is of size 1, the marginal is
      univariate.

    Returns:
      is_univariate: Boolean indicating whether the marginal is univariate or
      multivariate. In the case of dynamic shape in the number of index points,
      defaults to "multivariate" since that's the best we can do.
    """
    num_index_points = tf.compat.dimension_value(
        index_points.shape[-(self.kernel.feature_ndims + 1)])
    if num_index_points is None:
      warnings.warn(
          'Unable to detect statically whether the number of index_points is '
          '1. As a result, defaulting to treating the marginal Student T '
          'Process at `index_points` as a multivariate Student T. This makes '
          'some methods, like `cdf` unavailable.')
    return num_index_points == 1

  def _compute_covariance(self, index_points):
    kernel_matrix = self.kernel.matrix(index_points, index_points)
    if self._is_univariate_marginal(index_points):
      # kernel_matrix thus has shape [..., 1, 1]; squeeze off the last dims and
      # tack on the observation noise variance.
      return tf.squeeze(kernel_matrix, axis=[-2, -1])
    else:
      return kernel_matrix

  def get_marginal_distribution(self, index_points=None):
    """Compute the marginal over function values at `index_points`.

    Args:
      index_points: `float` `Tensor` representing finite (batch of) vector(s) of
        points in the index set over which the TP is defined. Shape has the form
        `[b1, ..., bB, e, f1, ..., fF]` where `F` is the number of feature
        dimensions and must equal `kernel.feature_ndims` and `e` is the number
        (size) of index points in each batch. Ultimately this distribution
        corresponds to a `e`-dimensional multivariate student t. The batch shape
        must be broadcastable with `kernel.batch_shape` and any batch dims
        yielded by `mean_fn`.

    Returns:
      marginal: a `StudentT` or `MultivariateStudentT` distribution,
        according to whether `index_points` consists of one or many index
        points, respectively.
    """
    with self._name_and_control_scope('get_marginal_distribution'):
      df = tf.convert_to_tensor(self.df)
      index_points = self._get_index_points(index_points)
      squared_scale = self._compute_covariance(index_points)
      if self._is_univariate_marginal(index_points):
        squared_scale = (df - 2.) / df * squared_scale
      else:
        squared_scale = ((df - 2.) / df)[
            ..., tf.newaxis, tf.newaxis] * squared_scale
      loc = self._mean_fn(index_points)
      # If we're sure the number of index points is 1, we can just construct a
      # scalar Normal. This has computational benefits and supports things like
      # CDF that aren't otherwise straightforward to provide.
      if self._is_univariate_marginal(index_points):
        scale = tf.sqrt(squared_scale)
        # `loc` has a trailing 1 in the shape; squeeze it.
        loc = tf.squeeze(loc, axis=-1)
        return student_t.StudentT(
            df=df,
            loc=loc,
            scale=scale,
            validate_args=self._validate_args,
            allow_nan_stats=self._allow_nan_stats,
            name='marginal_distribution')
      else:
        scale = tf.linalg.LinearOperatorLowerTriangular(
            tf.linalg.cholesky(_add_diagonal_shift(squared_scale, self.jitter)),
            is_non_singular=True,
            name='StudentTProcessScaleLinearOperator')
        return multivariate_student_t.MultivariateStudentTLinearOperator(
            df=df,
            loc=loc,
            scale=scale,
            validate_args=self._validate_args,
            allow_nan_stats=self._allow_nan_stats,
            name='marginal_distribution')

  @property
  def df(self):
    return self._df

  @property
  def mean_fn(self):
    return self._mean_fn

  @property
  def kernel(self):
    return self._kernel

  @property
  def index_points(self):
    return self._index_points

  @property
  def jitter(self):
    return self._jitter

  def _get_index_points(self, index_points=None):
    """Return `index_points` if not None, else `self._index_points`.

    Args:
      index_points: if given, this is what is returned; else,
      `self._index_points`

    Returns:
      index_points: the given arg, if not None, else the class member
      `self._index_points`.

    Rases:
      ValueError: if `index_points` and `self._index_points` are both `None`.
    """
    if self._index_points is None and index_points is None:
      raise ValueError(
          'This StudentTProcess instance was not instantiated with a value for '
          'index_points. One must therefore be provided when calling sample, '
          'log_prob, and other such methods.')
    return (index_points if index_points is not None
            else tf.convert_to_tensor(self._index_points))

  def _log_prob(self, value, index_points=None):
    return self.get_marginal_distribution(index_points).log_prob(value)

  def _batch_shape_tensor(self, index_points=None):
    index_points = self._get_index_points(index_points)
    return functools.reduce(tf.broadcast_dynamic_shape, [
        tf.shape(index_points)[:-(self.kernel.feature_ndims + 1)],
        self.kernel.batch_shape_tensor(),
        tf.shape(self.df)
    ])

  def _batch_shape(self, index_points=None):
    index_points = (
        index_points if index_points is not None else self._index_points)
    return functools.reduce(
        tf.broadcast_static_shape,
        [index_points.shape[:-(self.kernel.feature_ndims + 1)],
         self.kernel.batch_shape,
         self.df.shape])

  def _event_shape_tensor(self, index_points=None):
    index_points = self._get_index_points(index_points)
    if self._is_univariate_marginal(index_points):
      return tf.constant([], dtype=tf.int32)
    else:
      # The examples index is one position to the left of the feature dims.
      examples_index = -(self.kernel.feature_ndims + 1)
      return tf.shape(index_points)[examples_index:examples_index + 1]

  def _event_shape(self, index_points=None):
    index_points = (
        index_points if index_points is not None else self._index_points)
    if self._is_univariate_marginal(index_points):
      return tf.TensorShape([])
    else:
      # The examples index is one position to the left of the feature dims.
      examples_index = -(self.kernel.feature_ndims + 1)
      shape = index_points.shape[examples_index:examples_index + 1]
      if tensorshape_util.rank(shape) is None:
        return tf.TensorShape([None])
      return shape

  def _sample_n(self, n, seed=None, index_points=None):
    return self.get_marginal_distribution(index_points).sample(n, seed=seed)

  def _log_survival_function(self, value, index_points=None):
    return self.get_marginal_distribution(
        index_points).log_survival_function(value)

  def _survival_function(self, value, index_points=None):
    return self.get_marginal_distribution(index_points).survival_function(value)

  def _log_cdf(self, value, index_points=None):
    return self.get_marginal_distribution(index_points).log_cdf(value)

  def _entropy(self, index_points=None):
    return self.get_marginal_distribution(index_points).entropy()

  def _mean(self, index_points=None):
    return self.get_marginal_distribution(index_points).mean()

  def _quantile(self, value, index_points=None):
    return self.get_marginal_distribution(index_points).quantile(value)

  def _stddev(self, index_points=None):
    return tf.sqrt(self._variance(index_points=index_points))

  def _variance(self, index_points=None):
    index_points = self._get_index_points(index_points)

    kernel_diag = self.kernel.apply(index_points, index_points, example_ndims=1)
    if self._is_univariate_marginal(index_points):
      return tf.squeeze(kernel_diag, axis=[-1])
    return kernel_diag

  def _covariance(self, index_points=None):
    # Using the result of get_marginal_distribution would involve an extra
    # matmul, and possibly even an unnecessary cholesky first. We can avoid that
    # by going straight through the kernel function.
    return self._compute_covariance(self._get_index_points(index_points))

  def _mode(self, index_points=None):
    return self.get_marginal_distribution(index_points).mode()

  def _default_event_space_bijector(self):
    return identity_bijector.Identity(validate_args=self.validate_args)

  def _parameter_control_dependencies(self, is_init):
    if not self.validate_args:
      return []
    assertions = []
    if is_init != tensor_util.is_ref(self.df):
      assertions.append(
          assert_util.assert_greater(
              self.df, dtype_util.as_numpy_dtype(self.df.dtype)(2.),
              message='`df` must be greater than 2.'))
    return assertions

